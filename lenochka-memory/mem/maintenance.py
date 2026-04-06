import sys
from datetime import datetime, timedelta

from mem._config import DB_PATH
from mem._db import get_db, _load_vec


def consolidate():
    """
    Консолидация памяти: decay + cluster (vec ANN) + merge + RAPTOR + cleanup.

    Архитектурное решение: вместо O(n²) brute-force сравнения всех пар,
    используем sqlite-vec ANN для поиска top-K соседей каждого memory.
    500 записей × 10 соседей × 0.24мс = 1.2с вместо 46 минут.
    """
    conn = get_db()

    # 1. DECAY — ослабляем неиспользуемые memories (>7 дней)
    conn.execute("""
        UPDATE memories
        SET strength = MAX(0.1, strength * 0.95)
        WHERE last_accessed_at < datetime('now', '-7 days')
    """)
    conn.commit()

    # 2. MERGE через vec ANN (критический фикс: было O(n²), стало O(n·k))
    memories = conn.execute("""
        SELECT id, content, type, importance, strength
        FROM memories
        ORDER BY created_at DESC
        LIMIT 500
    """).fetchall()

    merged_count = 0
    vec_available = False

    try:
        if _load_vec(conn):
            vec_available = True
    except Exception:
        pass

    if vec_available and memories:
        try:
            from brain import embed_text, vec_to_blob, embed_texts_batch

            # Batch-эмбеддинг всех memories за один forward pass
            contents = [m["content"] for m in memories]
            ids = [m["id"] for m in memories]
            id_set = set(ids)

            try:
                vecs = embed_texts_batch(contents)
            except Exception:
                vecs = [embed_text(c) for c in contents]

            # Для каждого memory ищем top-10 ближайших через sqlite-vec
            # и мержим дубли (sim > 0.85)
            processed_pairs = set()

            for i, (mid, vec, mem_row) in enumerate(zip(ids, vecs, memories)):
                vec_blob = vec_to_blob(vec)

                try:
                    neighbors = conn.execute(
                        """
                        SELECT vm.rowid as nid, m.content, m.type, m.importance,
                               distance
                        FROM vec_memories vm
                        JOIN memories m ON vm.rowid = m.id
                        WHERE vm.embedding MATCH ? AND k = 11
                        ORDER BY distance
                    """,
                        (vec_blob,),
                    ).fetchall()
                except Exception:
                    continue

                for nb in neighbors:
                    nid = nb["nid"]
                    if nid == mid or nid not in id_set:
                        continue

                    pair_key = (min(mid, nid), max(mid, nid))
                    if pair_key in processed_pairs:
                        continue
                    processed_pairs.add(pair_key)

                    sim = 1.0 - nb["distance"] if nb["distance"] is not None else 0
                    if sim <= 0.85:
                        continue

                    if nb["type"] != mem_row["type"]:
                        continue

                    # Определяем что оставить (higher importance wins)
                    if mem_row["importance"] >= nb["importance"]:
                        keep_id, drop_id = mid, nid
                    else:
                        keep_id, drop_id = nid, mid

                    # Переносим ассоциации с drop на keep
                    conn.execute(
                        """
                        UPDATE associations
                        SET memory_id_from = ?
                        WHERE memory_id_from = ? AND memory_id_to != ?
                    """,
                        (keep_id, drop_id, keep_id),
                    )
                    conn.execute(
                        """
                        UPDATE associations
                        SET memory_id_to = ?
                        WHERE memory_id_to = ? AND memory_id_from != ?
                    """,
                        (keep_id, drop_id, keep_id),
                    )

                    # Удаляем memory
                    conn.execute("DELETE FROM memories WHERE id = ?", (drop_id,))
                    # Удаляем вектор (критично: иначе мусор в vec-поиске)
                    conn.execute("DELETE FROM vec_memories WHERE rowid = ?", (drop_id,))

                    # Убираем из id_set чтобы не мержить уже удалённое
                    id_set.discard(drop_id)
                    merged_count += 1

            conn.commit()

        except Exception as e:
            print(f"⚠️ Vec ANN merge error: {e}", file=sys.stderr)
            conn.rollback()

    elif memories:
        # Fallback: если vec недоступен, используем similarity pairwise
        # но с лимитом — только последние 50 записей (чтобы не зависнуть)
        try:
            from brain import similarity as _sim

            small_set = memories[:50]
            processed_pairs = set()

            for i, m1 in enumerate(small_set):
                for m2 in small_set[i + 1 :]:
                    pair_key = (min(m1["id"], m2["id"]), max(m1["id"], m2["id"]))
                    if pair_key in processed_pairs:
                        continue
                    processed_pairs.add(pair_key)

                    sim = _sim(m1["content"], m2["content"])
                    if sim > 0.85 and m1["type"] == m2["type"]:
                        if m1["importance"] >= m2["importance"]:
                            keep_id, drop_id = m1["id"], m2["id"]
                        else:
                            keep_id, drop_id = m2["id"], m1["id"]

                        conn.execute(
                            "UPDATE associations SET memory_id_from = ? WHERE memory_id_from = ? AND memory_id_to != ?",
                            (keep_id, drop_id, keep_id),
                        )
                        conn.execute(
                            "UPDATE associations SET memory_id_to = ? WHERE memory_id_to = ? AND memory_id_from != ?",
                            (keep_id, drop_id, keep_id),
                        )
                        conn.execute("DELETE FROM memories WHERE id = ?", (drop_id,))
                        merged_count += 1

            conn.commit()
        except ImportError:
            pass

    # 3. CLUSTER — ассоциации для свежих memories через vec ANN
    cluster_count = 0
    try:
        from brain import auto_associate as _auto_assoc

        recent = conn.execute("""
            SELECT id, content FROM memories
            WHERE created_at > datetime('now', '-30 days')
            ORDER BY created_at DESC LIMIT 100
        """).fetchall()
        for m in recent:
            assocs = _auto_assoc(m["id"], m["content"])
            if assocs:
                cluster_count += len(assocs)
    except ImportError:
        pass

    # 4. RAPTOR rebuild
    raptor_count = 0
    try:
        from brain import build_raptor

        raptor_count = build_raptor(level=0, batch_size=8)
        build_raptor(level=1, batch_size=5)
    except ImportError:
        pass

    # 5. Cleanup слабых memories + чистим vec (критично: без этого мусор в поиске)
    weak_ids = conn.execute("""
        SELECT id FROM memories
        WHERE strength < 0.15 AND importance < 0.3
    """).fetchall()

    deleted = 0
    for row in weak_ids:
        conn.execute("DELETE FROM vec_memories WHERE rowid = ?", (row["id"],))
        conn.execute(
            "DELETE FROM associations WHERE memory_id_from = ? OR memory_id_to = ?",
            (row["id"], row["id"]),
        )
        conn.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
        deleted += 1

    conn.commit()
    conn.close()

    print(f"✅ Консолидация завершена:")
    print(f"   Decay: применён")
    print(f"   Merge: {merged_count} дублей объединено (vec ANN)")
    print(f"   Cluster: {cluster_count} новых ассоциаций")
    print(f"   RAPTOR: {raptor_count} leaf-нод создано")
    print(f"   Cleanup: {deleted} слабых memories удалено (vec+assoc очищены)")


def prune_messages(older_than_days=180):
    """Архивация/удаление старых сообщений."""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(days=older_than_days)).strftime("%Y-%m-%d")
    deleted = conn.execute("DELETE FROM messages WHERE sent_at < ?", (cutoff,)).rowcount
    conn.commit()
    conn.close()
    print(f"✅ Архивировано {deleted} сообщений старше {older_than_days} дней")


def stats():
    """Статистика базы данных."""
    conn = get_db()
    tables = [
        "contacts",
        "companies",
        "chat_threads",
        "messages",
        "leads",
        "deals",
        "tasks",
        "agreements",
        "invoices",
        "payments",
        "memories",
        "associations",
        "raptor_nodes",
    ]
    print("📊 Lenochka Memory v2 — Статистика:")
    print(f"   БД: {DB_PATH}")
    print(
        f"   Размер: {DB_PATH.stat().st_size / 1024:.1f} KB"
        if DB_PATH.exists()
        else "   (не создана)"
    )
    print()
    for t in tables:
        try:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {t}").fetchone()
            print(f"   {t:25s} → {row['cnt']} записей")
        except Exception:
            print(f"   {t:25s} → (ошибка)")

    # Векторные таблицы
    try:
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        for vt in ["vec_memories"]:
            try:
                row = conn.execute(f"SELECT COUNT(*) as cnt FROM {vt}").fetchone()
                print(f"   {vt:25s} → {row['cnt']} векторов")
            except Exception:
                print(f"   {vt:25s} → (не создана)")
    except Exception:
        print("   sqlite-vec → недоступен")
    conn.close()
