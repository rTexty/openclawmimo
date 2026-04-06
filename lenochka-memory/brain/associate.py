"""Автосвязывание memories через векторные эмбеддинги."""

import sqlite3

from brain._config import DB_PATH
from brain._db import _get_db
from brain.embed import cosine_similarity, embed_text, vec_to_blob


def auto_associate(memory_id, content, threshold=0.35):
    """
    Найти похожие memories и создать ассоциации.
    Использует векторные эмбеддинги через sqlite-vec если доступны,
    иначе fallback на прямой cosine по содержимому.
    """
    conn = _get_db()
    associations = []

    try:
        try:
            conn.enable_load_extension(True)
            import sqlite_vec

            sqlite_vec.load(conn)
            conn.enable_load_extension(False)

            # Векторный поиск через sqlite-vec
            vec = embed_text(content)
            vec_blob = vec_to_blob(vec)

            rows = conn.execute(
                """
                SELECT vm.rowid as memory_id, m.content, m.type, m.importance,
                       distance
                FROM vec_memories vm
                JOIN memories m ON vm.rowid = m.id
                WHERE vm.embedding MATCH ? AND k = 20 AND vm.rowid != ?
                ORDER BY distance
            """,
                (vec_blob, memory_id),
            ).fetchall()

            for row in rows:
                sim = 1.0 - row["distance"] if "distance" in row.keys() else 0
                if sim < threshold:
                    continue
                rel_type = "supports" if sim > 0.8 else "related"

                existing = conn.execute(
                    """
                    SELECT id FROM associations
                    WHERE (memory_id_from = ? AND memory_id_to = ?)
                       OR (memory_id_from = ? AND memory_id_to = ?)
                """,
                    (memory_id, row["memory_id"], row["memory_id"], memory_id),
                ).fetchone()

                if not existing:
                    conn.execute(
                        """
                        INSERT INTO associations (memory_id_from, memory_id_to, relation_type, weight)
                        VALUES (?, ?, ?, ?)
                    """,
                        (memory_id, row["memory_id"], rel_type, round(sim, 3)),
                    )
                    associations.append(
                        {
                            "target_id": row["memory_id"],
                            "relation": rel_type,
                            "weight": round(sim, 3),
                        }
                    )
        except Exception:
            # Fallback: прямое сравнение cosine similarity
            emb_source = embed_text(content)
            rows = conn.execute(
                """
                SELECT id, content FROM memories
                WHERE id != ?
                ORDER BY created_at DESC
                LIMIT 200
            """,
                (memory_id,),
            ).fetchall()

            for row in rows:
                emb_target = embed_text(row["content"])
                sim = cosine_similarity(emb_source, emb_target)

                if sim >= threshold:
                    rel_type = "supports" if sim > 0.8 else "related"

                    existing = conn.execute(
                        """
                        SELECT id FROM associations
                        WHERE (memory_id_from = ? AND memory_id_to = ?)
                           OR (memory_id_from = ? AND memory_id_to = ?)
                    """,
                        (memory_id, row["id"], row["id"], memory_id),
                    ).fetchone()

                    if not existing:
                        conn.execute(
                            """
                            INSERT INTO associations (memory_id_from, memory_id_to, relation_type, weight)
                            VALUES (?, ?, ?, ?)
                        """,
                            (memory_id, row["id"], rel_type, round(sim, 3)),
                        )
                        associations.append(
                            {
                                "target_id": row["id"],
                                "relation": rel_type,
                                "weight": round(sim, 3),
                            }
                        )

        conn.commit()
    finally:
        conn.close()

    return associations
