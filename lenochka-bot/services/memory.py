"""
Memory Service — обёртка над mem.py для bot-специфичных операций.
Dedup, store message, soft-delete, status queries.
"""
import sqlite3
import logging
import hashlib
from aiogram.types import Message
from services.brain_wrapper import get_db

logger = logging.getLogger("lenochka.memory")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def dedup_check(msg: Message, normalized_text: str, db_path: str) -> str | None:
    """
    Проверка дубликата. Возвращает content_hash если НЕ дубликат, None если дубль.
    Проверяет: source_message_id + chat_id и content_hash.
    """
    ch = content_hash(normalized_text)
    conn = get_db(db_path)
    try:
        # Check source_message_id
        existing = conn.execute(
            """SELECT id FROM memories
               WHERE source_message_id = ? AND chat_thread_id = (
                   SELECT id FROM chat_threads WHERE tg_chat_id = ?
               )""",
            (msg.message_id, str(msg.chat.id)),
        ).fetchone()
        if existing:
            return None

        # Check content_hash (last 24h)
        existing = conn.execute(
            """SELECT id FROM memories
               WHERE content_hash = ? AND created_at > datetime('now', '-1 day')""",
            (ch,),
        ).fetchone()
        if existing:
            return None

        return ch
    finally:
        conn.close()


def store_message(chat_thread_id: int, from_user_id: str, text: str,
                  sent_at, content_type: str, meta: dict | None,
                  source_msg_id: int, content_hash_val: str,
                  db_path: str) -> int:
    """Сохранить raw message в CRM messages table."""
    import json
    conn = get_db(db_path)
    try:
        conn.execute(
            """INSERT INTO messages (chat_thread_id, from_user_id, text, sent_at,
                                     classification, meta_json)
               VALUES (?, ?, ?, datetime(?, 'unixepoch'), ?, ?)""",
            (chat_thread_id, from_user_id, text, sent_at,
             None, json.dumps(meta) if meta else None),
        )
        mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return mid
    finally:
        conn.close()


def soft_delete_messages(chat_id: int, message_ids: list[int], db_path: str):
    """Soft-delete: помечаем удалённые сообщения."""
    conn = get_db(db_path)
    try:
        placeholders = ",".join("?" * len(message_ids))
        conn.execute(
            f"""UPDATE messages SET meta_json = json_set(
                    COALESCE(meta_json, '{{}}'), '$.deleted', 1
                )
                WHERE id IN ({placeholders})""",
            message_ids,
        )
        conn.commit()
    finally:
        conn.close()


def get_business_status(user_id: int, db_path: str) -> dict:
    """Статус business-подключения."""
    # Пока заглушка — будет таблица business_connections
    return {"connected": False}


def register_business_connection(user_id: int, connection_id: str,
                                  can_reply: bool, can_read: bool, db_path: str):
    """Зарегистрировать подключение business-аккаунта."""
    logger.info(f"Business connected: user={user_id}, conn={connection_id}, "
                f"can_reply={can_reply}, can_read={can_read}")


def revoke_business_connection(connection_id: str, db_path: str):
    """Отозвать подключение."""
    logger.info(f"Business revoked: conn={connection_id}")


def get_status_summary(db_path: str) -> dict:
    """Сводка для /status команды."""
    conn = get_db(db_path)
    try:
        return {
            "messages_today": conn.execute(
                "SELECT COUNT(*) FROM messages WHERE sent_at > datetime('now', '-1 day')"
            ).fetchone()[0],
            "active_leads": conn.execute(
                "SELECT COUNT(*) FROM leads WHERE status NOT IN ('won', 'lost')"
            ).fetchone()[0],
            "open_deals": conn.execute(
                "SELECT COUNT(*) FROM deals WHERE stage NOT IN ('closed_won', 'closed_lost')"
            ).fetchone()[0],
            "open_tasks": conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done', 'cancelled')"
            ).fetchone()[0],
            "overdue_tasks": conn.execute(
                "SELECT COUNT(*) FROM v_overdue_tasks"
            ).fetchone()[0],
            "abandoned": 0,
            "total_memories": conn.execute(
                "SELECT COUNT(*) FROM memories"
            ).fetchone()[0],
        }
    finally:
        conn.close()


def get_active_leads(db_path: str) -> list[dict]:
    conn = get_db(db_path)
    try:
        rows = conn.execute(
            """SELECT l.*, c.name as contact_name FROM leads l
               JOIN contacts c ON l.contact_id = c.id
               WHERE l.status NOT IN ('won', 'lost')
               ORDER BY l.created_at DESC LIMIT 10"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_open_tasks(db_path: str) -> list[dict]:
    conn = get_db(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE status NOT IN ('done', 'cancelled')
               ORDER BY
                 CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
                 due_at ASC
               LIMIT 10"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_abandoned_dialogues(hours: int, db_path: str) -> list[dict]:
    conn = get_db(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM v_abandoned_dialogues WHERE hours_since > ?
               ORDER BY hours_since DESC LIMIT 10""",
            (hours,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_memory(query: str, db_path: str, brain=None) -> list[dict]:
    """Поиск по памяти (через brain.recall если доступен, иначе LIKE)."""
    if brain and brain.is_ready():
        return brain.recall(query=query, strategy="hybrid", limit=5)

    conn = get_db(db_path)
    try:
        rows = conn.execute(
            """SELECT id, content, type, importance
               FROM memories WHERE content LIKE ?
               ORDER BY importance DESC LIMIT 5""",
            (f"%{query}%",),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def run_consolidation(db_path: str, brain=None):
    """Запустить консолидацию памяти."""
    if brain and brain.is_ready():
        brain.consolidate()
        logger.info("Consolidation completed via brain")
    else:
        logger.warning("Brain not ready, skipping consolidation")
