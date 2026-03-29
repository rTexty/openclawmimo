"""
Contact Resolver — маппинг Telegram user → CRM contact + chat_thread.
Upsert: создаёт если нет, обновляет если есть.
"""
import sqlite3
import logging
from aiogram.types import Message
from services.brain_wrapper import get_db

logger = logging.getLogger("lenochka.contacts")


def resolve_contact(msg: Message, source: str, db_path: str) -> tuple[int | None, int]:
    """
    Возвращает (contact_id, chat_thread_id).
    Upsert в contacts и chat_threads.
    """
    conn = get_db(db_path)
    try:
        chat_thread_id = _upsert_chat_thread(conn, msg, source)
        contact_id = _upsert_contact(conn, msg, source)
        conn.commit()
        return contact_id, chat_thread_id
    finally:
        conn.close()


def _upsert_contact(conn: sqlite3.Connection, msg: Message,
                     source: str) -> int | None:
    """
    Telegram user → CRM contact (upsert).
    
    ВАЖНО: сохраняем ОБЕ стороны диалога.
    - sender_business_bot заполнен → это сообщение от владельца (Камиля)
    - sender_business_bot пуст → это сообщение от собеседника (клиента)
    """
    user = msg.from_user
    if not user:
        return None

    is_owner_msg = bool(msg.sender_business_bot)

    tg_id = str(user.id)
    username = user.username
    name = f"{user.first_name or ''} {user.last_name or ''}".strip() or f"User {tg_id}"

    # Для сообщений владельца — добавляем метку
    if is_owner_msg:
        name = f"{name} (владелец)"

    # Ищем по tg_user_id, затем tg_username
    existing = None

    existing = conn.execute(
        "SELECT id FROM contacts WHERE tg_user_id = ?", (tg_id,)
    ).fetchone()

    if not existing and username:
        existing = conn.execute(
            "SELECT id FROM contacts WHERE tg_username = ?", (username,)
        ).fetchone()

    if existing:
        # Update name если изменился
        conn.execute(
            "UPDATE contacts SET name = ?, updated_at = datetime('now') WHERE id = ?",
            (name, existing["id"]),
        )
        return existing["id"]

    # Create new
    conn.execute(
        """INSERT INTO contacts (name, tg_username, tg_user_id, notes)
           VALUES (?, ?, ?, ?)""",
        (name, username, tg_id, f"tg_id:{tg_id}"),
    )
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    logger.info(f"New contact: #{cid} {name} (@{username})")
    return cid


def _upsert_chat_thread(conn: sqlite3.Connection, msg: Message,
                         source: str) -> int:
    """Chat → CRM chat_thread (upsert)."""
    chat = msg.chat
    tg_chat_id = str(chat.id)

    existing = conn.execute(
        "SELECT id FROM chat_threads WHERE tg_chat_id = ?", (tg_chat_id,)
    ).fetchone()

    if existing:
        return existing["id"]

    chat_type = chat.type
    title = chat.title or ""
    if chat.first_name:
        title = f"{chat.first_name} {chat.last_name or ''}".strip()

    conn.execute(
        """INSERT INTO chat_threads (tg_chat_id, type, title)
           VALUES (?, ?, ?)""",
        (tg_chat_id, chat_type, title),
    )
    ctid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    logger.info(f"New chat_thread: #{ctid} [{chat_type}] {title}")
    return ctid
