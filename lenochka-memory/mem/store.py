import json
import sys
import hashlib

from mem._db import get_db, _load_vec


def _content_hash(text):
    """SHA-256 хэш контента для дедупликации."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def store(
    content,
    mem_type="episodic",
    importance=0.5,
    category="other",
    contact_id=None,
    chat_thread_id=None,
    deal_id=None,
    source_message_id=None,
    tags=None,
    content_hash=None,
    auto_associate=True,
):
    """Записать memory в Agent Memory + векторный эмбеддинг (одна транзакция)."""
    conn = get_db()
    tags_json = json.dumps(tags) if tags else None
    chash = content_hash or _content_hash(content)
    mid = None
    try:
        conn.execute(
            """
            INSERT INTO memories (content, content_hash, type, category, importance, strength, contact_id,
                                 chat_thread_id, deal_id, source_message_id, tags)
            VALUES (?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?)
        """,
            (
                content,
                chash,
                mem_type,
                category,
                importance,
                contact_id,
                chat_thread_id,
                deal_id,
                source_message_id,
                tags_json,
            ),
        )
        mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Векторный эмбеддинг → vec_memories (в той же транзакции)
        try:
            from brain import embed_text, vec_to_blob

            vec = embed_text(content)
            vec_blob = vec_to_blob(vec)
            if _load_vec(conn):
                conn.execute(
                    "INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)",
                    (mid, vec_blob),
                )
        except Exception as e:
            print(f"⚠️ Не удалось записать вектор: {e}", file=sys.stderr)
            # Вектор не критичен — memory остаётся, fallback поиск работает

        # Один COMMIT на обе операции (memory + vector)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"❌ store() failed: {e}", file=sys.stderr)
        conn.close()
        raise
    conn.close()
    print(f"✅ Memory #{mid} записан [{mem_type}/{category}] importance={importance}")

    # Автосвязывание (вне транзакции — не критично)
    if auto_associate:
        try:
            from brain import auto_associate as _auto_assoc

            assocs = _auto_assoc(mid, content)
            if assocs:
                print(f"   🔗 Создано {len(assocs)} ассоциаций")
        except Exception:
            pass

    return mid
