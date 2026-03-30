"""
Response Context — сбор контекста для LLM-вызовов.

Извлечён из pipeline.py для чистой архитектуры.

Компоненты:
1. build_chat_context() — последние N сообщений из чата для классификации
2. build_crm_context() — данные CRM для extract_entities (contact, deals, tasks)
3. build_notification_context() — контекст для escalation notification owner'у
"""
import sqlite3
import logging

logger = logging.getLogger("lenochka.context")


# =========================================================
# 1. CHAT CONTEXT — для classify и response decision
# =========================================================

def build_chat_context(chat_thread_id: int | None, db_path: str,
                       limit: int = 5) -> str:
    """
    Контекст последних N сообщений из чата.
    Используется для классификации коротких ответов ('да', 'нет', 'ок'):
    reply на '150к?' = decision, reply на 'как дела?' = chit-chat.
    """
    if not chat_thread_id:
        return ""

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT text, from_user_id, sent_at FROM messages
               WHERE chat_thread_id = ?
                 AND (meta_json IS NULL OR json_extract(meta_json, '$.deleted') IS NULL)
               ORDER BY sent_at DESC LIMIT ?""",
            (chat_thread_id, limit),
        ).fetchall()
    except Exception:
        return ""
    finally:
        conn.close()

    if not rows:
        return ""

    lines = []
    for r in reversed(rows):
        author = "Я" if r["from_user_id"] == "self" else "Клиент"
        text = (r["text"] or "")[:100]
        lines.append(f"[{author}: {text}]")
    return " ".join(lines)


# =========================================================
# 2. CRM CONTEXT — для extract_entities
# =========================================================

def build_crm_context(chat_ctx: str, contact_id: int | None,
                      chat_thread_id: int | None, db_path: str) -> str:
    """
    Обогатить контекст для extract_entities.
    Добавляет существующую информацию о контакте, сделках и задачах,
    чтобы LLM корректно извлекал сущности (не дублировал, не терял связь).
    """
    parts = [chat_ctx] if chat_ctx else []

    if not contact_id:
        return " ".join(parts)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Contact info
        contact = conn.execute(
            "SELECT name, tg_username, company_id FROM contacts WHERE id = ?",
            (contact_id,),
        ).fetchone()
        if contact:
            info = f"Контакт: {contact['name']}"
            if contact["tg_username"]:
                info += f" (@{contact['tg_username']})"
            if contact["company_id"]:
                comp = conn.execute(
                    "SELECT name FROM companies WHERE id = ?",
                    (contact["company_id"],),
                ).fetchone()
                if comp:
                    info += f", {comp['name']}"
            parts.append(info)

        # Active deals
        deals = conn.execute(
            """SELECT amount, stage FROM deals
               WHERE contact_id = ? AND stage NOT IN ('closed_won', 'closed_lost')
               ORDER BY created_at DESC LIMIT 3""",
            (contact_id,),
        ).fetchall()
        for d in deals:
            amt = f"{d['amount']:,.0f}₽" if d["amount"] else "сумма не указана"
            parts.append(f"Активная сделка: {amt}, стадия: {d['stage']}")

        # Open tasks
        tasks = conn.execute(
            """SELECT description, priority, due_at FROM tasks
               WHERE related_type = 'contact' AND related_id = ?
                 AND status NOT IN ('done', 'cancelled')
               ORDER BY due_at ASC LIMIT 3""",
            (contact_id,),
        ).fetchall()
        for t in tasks:
            due = f" (до {t['due_at'][:10]})" if t.get("due_at") else ""
            parts.append(f"Задача: {t['description'][:60]}{due}")

    except Exception:
        pass
    finally:
        conn.close()

    return " | ".join(parts)


# =========================================================
# 3. NOTIFICATION CONTEXT — для escalation owner'у
# =========================================================

def build_notification_context(chat_thread_id: int | None,
                                contact_id: int | None,
                                db_path: str) -> dict:
    """
    Собрать контекст для уведомления owner'у об эскалации.
    Возвращает dict с contact_name, deals, tasks, recent_messages.
    """
    result = {
        "contact_name": "Клиент",
        "deals": [],
        "tasks": [],
        "recent_messages": [],
    }

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Contact name
        if contact_id:
            row = conn.execute(
                "SELECT name FROM contacts WHERE id = ?", (contact_id,)
            ).fetchone()
            if row:
                result["contact_name"] = row["name"]

            # Active deals
            deals = conn.execute("""
                SELECT amount, stage FROM deals
                WHERE contact_id = ? AND stage NOT IN ('closed_won', 'closed_lost')
                ORDER BY updated_at DESC LIMIT 2
            """, (contact_id,)).fetchall()
            for d in deals:
                amt = f"{d['amount']:,.0f}₽" if d.get("amount") else "?"
                result["deals"].append(f"💰 Сделка: {amt} ({d['stage']})")

            # Open tasks
            tasks = conn.execute("""
                SELECT description, due_at, priority FROM tasks
                WHERE related_type = 'contact' AND related_id = ?
                  AND status NOT IN ('done', 'cancelled')
                ORDER BY due_at ASC LIMIT 3
            """, (contact_id,)).fetchall()
            for t in tasks:
                due = f" до {t['due_at'][:10]}" if t.get("due_at") else ""
                result["tasks"].append(f"📋 {t['description'][:50]}{due}")

        # Recent messages
        if chat_thread_id:
            msgs = conn.execute("""
                SELECT text, from_user_id, sent_at FROM messages
                WHERE chat_thread_id = ?
                  AND (meta_json IS NULL OR json_extract(meta_json, '$.deleted') IS NULL)
                ORDER BY sent_at DESC LIMIT 3
            """, (chat_thread_id,)).fetchall()
            for m in reversed(msgs):
                who = "Я" if m["from_user_id"] == "self" else "Клиент"
                result["recent_messages"].append(f"  {who}: {m['text'][:60]}")

    except Exception:
        pass
    finally:
        conn.close()

    return result


def format_context_block(ctx: dict) -> str:
    """Форматировать контекст в читаемый блок."""
    parts = []

    if ctx.get("deals"):
        parts.extend(ctx["deals"])
    if ctx.get("tasks"):
        parts.extend(ctx["tasks"])
    if ctx.get("recent_messages"):
        parts.append("💬 Последние сообщения:\n" + "\n".join(ctx["recent_messages"]))

    return "\n".join(parts) if parts else "нет контекста"
