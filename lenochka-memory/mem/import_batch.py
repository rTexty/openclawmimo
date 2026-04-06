import hashlib
import json
import sys
from datetime import datetime

from mem._db import get_db


def import_batch(
    messages: list[dict],
    contact_id: int | None,
    chat_thread_id: int,
) -> dict:
    """
    Batch-import parsed messages into the database.

    messages: [{"from_name": str, "text": str, "date": str, "has_reply": bool}, ...]
    Returns: {"messages_inserted": int, "memories_created": int}
    """
    conn = get_db()
    messages_inserted = 0
    memories_created = 0

    try:
        for msg in messages:
            from_name = msg.get("from_name", "Unknown")
            text = msg.get("text", "")
            date_str = msg.get("date", datetime.now().isoformat())

            conn.execute(
                """INSERT INTO messages
                   (chat_thread_id, from_user_id, text, sent_at,
                    content_hash, analyzed, classification)
                   VALUES (?, ?, ?, ?, ?, 1, 'chit-chat')""",
                (
                    chat_thread_id,
                    from_name,
                    text[:4096],
                    date_str[:19] if len(date_str) > 19 else date_str,
                    hashlib.sha256(
                        f"{from_name}:{text}:{date_str}".encode()
                    ).hexdigest()[:16],
                ),
            )
            messages_inserted += 1
            msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            if text.strip():
                content_hash = hashlib.sha256(text.encode()).hexdigest()

                existing = conn.execute(
                    "SELECT id FROM memories WHERE content_hash=?", (content_hash,)
                ).fetchone()
                if existing:
                    continue

                conn.execute(
                    """INSERT INTO memories
                       (content, content_hash, type, importance, strength,
                        contact_id, chat_thread_id, source_message_id)
                       VALUES (?, ?, 'episodic', 0.3, 1.0, ?, ?, ?)""",
                    (text, content_hash, contact_id, chat_thread_id, msg_id),
                )
                memories_created += 1
                mem_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                try:
                    conn.execute(
                        "INSERT INTO memories_fts(rowid, content, category) VALUES (?, ?, ?)",
                        (mem_id, text, "import"),
                    )
                except Exception:
                    pass

                try:
                    conn.execute(
                        """INSERT INTO chaos_entries
                           (memory_id, content, category, timestamp)
                           VALUES (?, ?, 'import', ?)""",
                        (
                            mem_id,
                            text[:200],
                            date_str[:19] if len(date_str) > 19 else date_str,
                        ),
                    )
                except Exception:
                    pass

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise

    conn.close()
    return {
        "messages_inserted": messages_inserted,
        "memories_created": memories_created,
    }
