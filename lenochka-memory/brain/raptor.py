"""RAPTOR — иерархическая суммаризация memories."""

import json

from brain._db import _get_db
from brain.embed import embed_text, vec_to_blob
from brain.llm import _call_llm


def build_raptor(level=0, batch_size=10):
    """Построить RAPTOR-дерево."""
    conn = _get_db()

    if level == 0:
        memories = conn.execute("""
            SELECT id, content, type, importance
            FROM memories
            WHERE id NOT IN (
                SELECT DISTINCT value FROM raptor_nodes, json_each(raptor_nodes.memory_ids)
                WHERE raptor_nodes.memory_ids IS NOT NULL
            )
            ORDER BY importance DESC, created_at DESC
            LIMIT 100
        """).fetchall()

        if not memories:
            conn.close()
            return 0

        created = 0
        for i in range(0, len(memories), batch_size):
            batch = memories[i : i + batch_size]
            batch_ids = [m["id"] for m in batch]
            batch_contents = [m["content"] for m in batch]

            summary = _summarize_batch(batch_contents)
            conn.execute(
                """
                INSERT INTO raptor_nodes (level, summary, memory_ids)
                VALUES (?, ?, ?)
            """,
                (0, summary, json.dumps(batch_ids)),
            )
            created += 1

        conn.commit()
        conn.close()
        return created
    else:
        children = conn.execute(
            """
            SELECT id, summary, memory_ids FROM raptor_nodes
            WHERE level = ?
            ORDER BY id
        """,
            (level - 1,),
        ).fetchall()

        if len(children) < 2:
            conn.close()
            return 0

        created = 0
        for i in range(0, len(children), batch_size):
            batch = children[i : i + batch_size]
            child_ids = [c["id"] for c in batch]
            child_summaries = [c["summary"] for c in batch]

            all_memory_ids = []
            for c in batch:
                if c["memory_ids"]:
                    try:
                        all_memory_ids.extend(json.loads(c["memory_ids"]))
                    except (json.JSONDecodeError, TypeError):
                        pass

            summary = _summarize_batch(child_summaries)
            conn.execute(
                """
                INSERT INTO raptor_nodes (level, summary, memory_ids)
                VALUES (?, ?, ?)
            """,
                (
                    level,
                    summary,
                    json.dumps(all_memory_ids) if all_memory_ids else None,
                ),
            )

            parent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for cid in child_ids:
                conn.execute(
                    "UPDATE raptor_nodes SET parent_id = ? WHERE id = ?",
                    (parent_id, cid),
                )
            created += 1

        conn.commit()
        conn.close()
        return created


def _summarize_batch(texts):
    """Суммаризировать batch текстов."""
    combined = "\n".join(f"- {t}" for t in texts[:10])

    result = _call_llm(
        "Ты — система суммаризации. Сверни список фактов в 1-2 кратких предложения.",
        f"Факты:\n{combined}\n\nКраткая суммаризация:",
        max_tokens=200,
    )

    if result:
        return result
    return "; ".join(t[:80] for t in texts[:3])
