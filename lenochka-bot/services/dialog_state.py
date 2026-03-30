"""
Dialog State — проверка состояния диалога перед escalation.

Проверяет:
- Ответил ли owner в бизнес-чате после вопроса клиента
- Новый ли контакт (первое взаимодействие)
- Есть ли активная переписка (owner и клиент переписываются)
"""
import sqlite3
import logging
from services.brain_wrapper import get_db

logger = logging.getLogger("lenochka.dialog_state")


def get_dialog_state(chat_thread_id: int | None,
                     message_id: int | None,
                     db_path: str) -> dict:
    """
    Собрать состояние диалога для решения об эскалации.
    
    Возвращает:
    {
        "owner_replied_after": bool — owner ответил после этого сообщения?
        "contact_is_new": bool — первый контакт?
        "recent_owner_messages": int — сколько сообщений owner'а за последние 2ч
        "recent_client_messages": int — сколько сообщений клиента за последние 2ч
    }
    """
    result = {
        "owner_replied_after": False,
        "contact_is_new": False,
        "recent_owner_messages": 0,
        "recent_client_messages": 0,
    }

    if not chat_thread_id:
        return result

    conn = get_db(db_path)
    try:
        # 1. Owner replied after this message?
        if message_id:
            row = conn.execute("""
                SELECT COUNT(*) as cnt FROM messages
                WHERE chat_thread_id = ?
                  AND from_user_id = 'self'
                  AND id > ?
                  AND (meta_json IS NULL OR json_extract(meta_json, '$.deleted') IS NULL)
            """, (chat_thread_id, message_id)).fetchone()
            result["owner_replied_after"] = (row["cnt"] > 0) if row else False

        # 2. Contact is new? (first message in this chat ever)
        row = conn.execute("""
            SELECT COUNT(*) as cnt FROM messages
            WHERE chat_thread_id = ?
              AND (meta_json IS NULL OR json_extract(meta_json, '$.deleted') IS NULL)
        """, (chat_thread_id,)).fetchone()
        total_msgs = row["cnt"] if row else 0
        result["contact_is_new"] = total_msgs <= 2

        # 3. Recent activity (last 2 hours)
        row = conn.execute("""
            SELECT
                SUM(CASE WHEN from_user_id = 'self' THEN 1 ELSE 0 END) as owner_cnt,
                SUM(CASE WHEN from_user_id != 'self' THEN 1 ELSE 0 END) as client_cnt
            FROM messages
            WHERE chat_thread_id = ?
              AND sent_at > datetime('now', '-2 hours')
              AND (meta_json IS NULL OR json_extract(meta_json, '$.deleted') IS NULL)
        """, (chat_thread_id,)).fetchone()
        if row:
            result["recent_owner_messages"] = row["owner_cnt"] or 0
            result["recent_client_messages"] = row["client_cnt"] or 0

    except Exception as e:
        logger.error(f"get_dialog_state error: {e}")
    finally:
        conn.close()

    return result


def is_owner_message(msg) -> bool:
    """Проверить, является ли сообщение от owner'а (через business API)."""
    if hasattr(msg, 'sender_business_bot') and msg.sender_business_bot:
        return True
    if hasattr(msg, 'from_user') and msg.from_user and msg.from_user.is_bot:
        return True
    return False
