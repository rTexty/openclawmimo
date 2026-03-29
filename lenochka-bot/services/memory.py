"""
Memory Service — обёртка над mem.py для bot-специфичных операций.
Dedup, supersede, soft-delete, store message, status queries, business connections.

Ключевое архитектурное решение: source_msg_id — отдельная колонка в messages,
а НЕ кусок meta_json. Это даёт UNIQUE index для дедупликации и прямой lookup
для supersede (edited) и soft-delete (deleted).
"""
import sqlite3
import json
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
    Источники дублей: Telegram retry, restart long-polling, ручной ingest.
    
    Проверяет:
    1. source_msg_id + chat_thread_id (UNIQUE index — O(1) lookup)
    2. content_hash за последние 24ч (fuzzy дедуп)
    """
    ch = content_hash(normalized_text)
    conn = get_db(db_path)
    try:
        # 1. Telegram message_id уникален в рамках чата
        existing = conn.execute(
            """SELECT m.id FROM messages m
               JOIN chat_threads ct ON m.chat_thread_id = ct.id
               WHERE ct.tg_chat_id = ? AND m.source_msg_id = ?
               LIMIT 1""",
            (str(msg.chat.id), msg.message_id),
        ).fetchone()
        if existing:
            return None

        # 2. Content hash — защита от одинаковых текстов в разных чатах
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
    """Сохранить raw message в CRM messages table с source_msg_id."""
    conn = get_db(db_path)
    try:
        conn.execute(
            """INSERT INTO messages (chat_thread_id, from_user_id, text, sent_at,
                                     classification, analyzed, source_msg_id, meta_json)
               VALUES (?, ?, ?, datetime(?, 'unixepoch'), ?, 0, ?, ?)""",
            (chat_thread_id, from_user_id, text, sent_at,
             None, source_msg_id, json.dumps(meta) if meta else None),
        )
        mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return mid
    finally:
        conn.close()


def supersede_message(chat_thread_id: int, source_msg_id: int,
                      new_text: str, new_meta: dict | None, db_path: str) -> int | None:
    """
    Supersede: обновить существующее сообщение при редактировании.
    
    Ищем message по (chat_thread_id, source_msg_id):
      - Найдена → UPDATE text, сброс classification/analyzed для повторной обработки
      - Не найдена → возвращаем None, pipeline создаёт как новое
    
    Архитектурное решение: обновляем ТОЛЬКО messages.text и сбрасываем флаги.
    memories и chaos_entries НЕ трогаем — они хранят уже классифицированный контент.
    Переклассификация через nightly consolidate или ручную /reclassify.
    """
    conn = get_db(db_path)
    try:
        row = conn.execute(
            """SELECT id, text FROM messages
               WHERE chat_thread_id = ? AND source_msg_id = ?""",
            (chat_thread_id, source_msg_id),
        ).fetchone()

        if not row:
            return None

        msg_id = row["id"]

        conn.execute(
            """UPDATE messages
               SET text = ?, classification = NULL, analyzed = 0,
                   meta_json = json_set(
                       COALESCE(meta_json, '{}'),
                       '$.edited', 1,
                       '$.prev_text', ?
                   )
               WHERE id = ?""",
            (new_text, row["text"], msg_id),
        )
        conn.commit()
        logger.info(f"Supersede: msg#{msg_id} edited (source_msg_id={source_msg_id})")
        return msg_id
    finally:
        conn.close()


def soft_delete_messages(chat_id: int, message_ids: list[int], db_path: str):
    """
    Soft-delete: помечаем удалённые сообщения через source_msg_id.
    
    Telegram шлёт deleted_business_messages с message_ids (Telegram message_id).
    Lookup: (chat_thread_id, source_msg_id) → UNIQUE index, O(1).
    
    НЕ физически удаляем — FK memories.source_message_id → messages.id
    должен оставаться валидным. Физическая архивация — отдельный cron >1 года.
    """
    if not message_ids:
        return
    conn = get_db(db_path)
    try:
        for tg_msg_id in message_ids:
            conn.execute(
                """UPDATE messages SET meta_json = json_set(
                        COALESCE(meta_json, '{}'), '$.deleted', 1
                   )
                   WHERE chat_thread_id = (
                       SELECT id FROM chat_threads WHERE tg_chat_id = ?
                   ) AND source_msg_id = ?""",
                (str(chat_id), tg_msg_id),
            )
        conn.commit()
        logger.info(f"Soft-deleted {len(message_ids)} messages in chat {chat_id}")
    finally:
        conn.close()


# === Business Connections ===

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
    Критично для мульти-юзера: определяем кому принадлежит входящее сообщение.
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


# === Status / Search Queries ===

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
