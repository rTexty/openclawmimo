"""
Memory Service — обёртка над mem.py для bot-специфичных операций.
Dedup, store message, soft-delete, status queries, business connections.
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
    Проверяет: source_message_id + chat_id и content_hash (за 24ч).
    """
    ch = content_hash(normalized_text)
    conn = get_db(db_path)
    try:
        # Check source_message_id + chat_thread_id
        existing = conn.execute(
            """SELECT m.id FROM messages m
               JOIN chat_threads ct ON m.chat_thread_id = ct.id
               WHERE ct.tg_chat_id = ? AND m.meta_json LIKE ?
               LIMIT 1""",
            (str(msg.chat.id), f'%"{msg.message_id}"%'),
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
        # Сохраняем source_msg_id в meta_json для дедупликации
        full_meta = meta or {}
        full_meta["_source_msg_id"] = source_msg_id
        full_meta["_content_hash"] = content_hash_val

        conn.execute(
            """INSERT INTO messages (chat_thread_id, from_user_id, text, sent_at,
                                     classification, analyzed, meta_json)
               VALUES (?, ?, ?, datetime(?, 'unixepoch'), ?, 0, ?)""",
            (chat_thread_id, from_user_id, text, sent_at,
             None, json.dumps(full_meta)),
        )
        mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return mid
    finally:
        conn.close()


def soft_delete_messages(chat_id: int, message_ids: list[int], db_path: str):
    """
    Soft-delete: помечаем удалённые сообщения.
    
    Архитектурное решение: meta_json.deleted=1 вместо физического удаления.
    Это сохраняет целостность FK (memories.source_message_id → messages.id).
    Позже — отдельный cron для физической архивации старых deleted.
    """
    if not message_ids:
        return
    conn = get_db(db_path)
    try:
        # Ищем internal message id по source_msg_id в meta_json
        for msg_id in message_ids:
            conn.execute(
                """UPDATE messages SET meta_json = json_set(
                        COALESCE(meta_json, '{}'), '$.deleted', 1
                   )
                   WHERE meta_json LIKE ?""",
                (f'%"{msg_id}"%',),
            )
        conn.commit()
        logger.info(f"Soft-deleted {len(message_ids)} messages in chat {chat_id}")
    finally:
        conn.close()


def get_business_status(user_id: int, db_path: str) -> dict:
    """Статус business-подключения из реальной таблицы."""
    conn = get_db(db_path)
    try:
        row = conn.execute(
            """SELECT connection_id, status, can_reply, can_read_messages
               FROM business_connections
               WHERE owner_user_id = ? AND status = 'active'
               ORDER BY connected_at DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        if row:
            return {
                "connected": True,
                "connection_id": row["connection_id"],
                "can_reply": bool(row["can_reply"]),
                "can_read": bool(row["can_read_messages"]),
            }
        return {"connected": False}
    finally:
        conn.close()


def register_business_connection(user_id: int, connection_id: str,
                                  can_reply: bool, can_read: bool, db_path: str):
    """Зарегистрировать подключение business-аккаунта в БД."""
    conn = get_db(db_path)
    try:
        conn.execute("""
            INSERT INTO business_connections 
                (connection_id, owner_user_id, status, can_reply, can_read_messages)
            VALUES (?, ?, 'active', ?, ?)
            ON CONFLICT(connection_id) DO UPDATE SET
                status = 'active',
                can_reply = excluded.can_reply,
                can_read_messages = excluded.can_read_messages,
                revoked_at = NULL
        """, (connection_id, user_id, int(can_reply), int(can_read)))
        conn.commit()
        logger.info(
            f"Business connected: user={user_id}, conn={connection_id}, "
            f"can_reply={can_reply}, can_read={can_read}"
        )
    finally:
        conn.close()


def revoke_business_connection(connection_id: str, db_path: str):
    """Отозвать подключение."""
    conn = get_db(db_path)
    try:
        conn.execute("""
            UPDATE business_connections
            SET status = 'revoked', revoked_at = datetime('now')
            WHERE connection_id = ?
        """, (connection_id,))
        conn.commit()
        logger.info(f"Business revoked: conn={connection_id}")
    finally:
        conn.close()


def get_owner_by_connection(connection_id: str, db_path: str) -> int | None:
    """
    Маппинг business_connection_id → owner_user_id.
    Критично для определения кому принадлежит входящее сообщение
    при мульти-юзерной конфигурации.
    """
    conn = get_db(db_path)
    try:
        row = conn.execute(
            """SELECT owner_user_id FROM business_connections
               WHERE connection_id = ? AND status = 'active'""",
            (connection_id,),
        ).fetchone()
        return row["owner_user_id"] if row else None
    finally:
        conn.close()


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
