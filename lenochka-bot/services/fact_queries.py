"""
Fact Queries — SQL-запросы для извлечения фактов из CRM-БД.

Каждая функция: (contact_id, query_hint, chat_thread_id, db_path) → str | None
None = нет данных → escalate.
"""
import sqlite3
import logging
from services.brain_wrapper import get_db

logger = logging.getLogger("lenochka.facts")


def query_fact(intent: str, query_hint: str, contact_id: int | None,
               chat_thread_id: int | None, db_path: str) -> str | None:
    """Роутер: intent → конкретная SQL-функция."""
    queries = {
        "deadline":          query_deadline,
        "status":            query_status,
        "amount":            query_amount,
        "context_recall":    query_context,
        "payment_status":    query_payment_status,
        "overdue":           query_overdue,
        "tasks_today":       query_tasks_today,
        "active_leads":      query_leads_summary,
        "deal_details":      query_deal_details,
        "contact_history":   query_contact_history,
        "last_interaction":  query_last_interaction,
    }

    fn = queries.get(intent)
    if not fn:
        return None

    try:
        return fn(contact_id, query_hint, chat_thread_id, db_path)
    except Exception as e:
        logger.error(f"query_fact({intent}) error: {e}")
        return None


def query_deadline(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Когда договор? Когда КП? Когда оплата?"""
    if not contact_id:
        return None
    conn = get_db(db_path)
    parts = []

    # Agreements
    rows = conn.execute("""
        SELECT summary, due_at, status, amount FROM agreements
        WHERE contact_id = ? AND status NOT IN ('completed', 'cancelled')
        ORDER BY created_at DESC LIMIT 3
    """, (contact_id,)).fetchall()
    for r in rows:
        if r["status"] == "signed":
            parts.append(f"Договор «{r['summary'] or '—'}» подписан ✅")
        elif r["due_at"]:
            parts.append(f"Договор «{r['summary'] or '—'}» — до {r['due_at'][:10]}")

    # Tasks with due dates
    rows = conn.execute("""
        SELECT description, due_at, status FROM tasks
        WHERE related_type = 'contact' AND related_id = ?
          AND status NOT IN ('done', 'cancelled') AND due_at IS NOT NULL
        ORDER BY due_at ASC LIMIT 3
    """, (contact_id,)).fetchall()
    for r in rows:
        icon = "✅" if r["status"] == "done" else "📋"
        parts.append(f"{icon} {r['description'][:60]} — до {r['due_at'][:10]}")

    # Deals with expected close
    rows = conn.execute("""
        SELECT amount, stage, expected_close_at FROM deals
        WHERE contact_id = ? AND expected_close_at IS NOT NULL
          AND stage NOT IN ('closed_won', 'closed_lost')
        ORDER BY expected_close_at ASC LIMIT 2
    """, (contact_id,)).fetchall()
    for r in rows:
        amt = f"{r['amount']:,.0f}₽" if r.get("amount") else ""
        parts.append(f"Сделка {amt} — до {r['expected_close_at'][:10]} ({r['stage']})")

    conn.close()
    return "\n".join(parts) if parts else None


def query_status(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Что там с КП? Как дела с проектом?"""
    if not contact_id:
        return None
    conn = get_db(db_path)
    parts = []

    # Active deals
    rows = conn.execute("""
        SELECT amount, stage, notes, updated_at FROM deals
        WHERE contact_id = ? AND stage NOT IN ('closed_won', 'closed_lost')
        ORDER BY updated_at DESC LIMIT 2
    """, (contact_id,)).fetchall()
    for r in rows:
        amt = f"{r['amount']:,.0f}₽" if r.get("amount") else ""
        parts.append(f"Сделка {amt}: стадия «{r['stage']}»")

    # Open tasks
    rows = conn.execute("""
        SELECT description, status, priority, due_at FROM tasks
        WHERE related_type = 'contact' AND related_id = ?
          AND status NOT IN ('done', 'cancelled')
        ORDER BY
            CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,
            due_at ASC
        LIMIT 5
    """, (contact_id,)).fetchall()
    for r in rows:
        icon = {"open": "📋", "in_progress": "🔨"}.get(r["status"], "📋")
        due = f" (до {r['due_at'][:10]})" if r.get("due_at") else ""
        parts.append(f"{icon} {r['description'][:60]}{due}")

    # Recent memories
    rows = conn.execute("""
        SELECT content, created_at FROM memories
        WHERE contact_id = ?
        ORDER BY importance DESC, created_at DESC LIMIT 3
    """, (contact_id,)).fetchall()
    for r in rows:
        parts.append(f"📝 {r['content'][:80]}")

    conn.close()
    return "\n".join(parts) if parts else None


def query_amount(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Сколько договорились?"""
    if not contact_id:
        return None
    conn = get_db(db_path)
    row = conn.execute("""
        SELECT amount, stage, notes FROM deals
        WHERE contact_id = ? AND amount IS NOT NULL
          AND stage NOT IN ('closed_won', 'closed_lost')
        ORDER BY updated_at DESC LIMIT 1
    """, (contact_id,)).fetchone()
    conn.close()

    if row:
        return f"Сумма: {row['amount']:,.0f}₽, стадия: {row['stage']}"
    return None


def query_context(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Напомни о чём говорили / что решили."""
    conn = get_db(db_path)
    parts = []

    # Recent messages in chat
    if chat_thread_id:
        rows = conn.execute("""
            SELECT text, from_user_id, sent_at FROM messages
            WHERE chat_thread_id = ?
              AND (meta_json IS NULL OR json_extract(meta_json, '$.deleted') IS NULL)
            ORDER BY sent_at DESC LIMIT 10
        """, (chat_thread_id,)).fetchall()
        for r in reversed(rows):
            author = "Я" if r["from_user_id"] == "self" else "Клиент"
            parts.append(f"{author}: {r['text'][:100]}")

    # Key memories
    if contact_id:
        rows = conn.execute("""
            SELECT content, type, created_at FROM memories
            WHERE contact_id = ? AND importance >= 0.6
            ORDER BY created_at DESC LIMIT 5
        """, (contact_id,)).fetchall()
        for r in rows:
            parts.append(f"📌 {r['content'][:100]}")

    conn.close()
    return "\n".join(parts) if parts else None


def query_payment_status(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Статус оплаты: неоплаченные счета, последние платежи."""
    if not contact_id:
        return None
    conn = get_db(db_path)
    parts = []

    # Неоплаченные счета
    rows = conn.execute("""
        SELECT i.amount, i.due_at, i.status, a.summary
        FROM invoices i
        JOIN agreements a ON i.agreement_id = a.id
        WHERE a.contact_id = ? AND i.status IN ('sent', 'overdue')
        ORDER BY i.due_at ASC
    """, (contact_id,)).fetchall()
    for r in rows:
        overdue = " ⚠️ просрочен" if r["status"] == "overdue" else ""
        parts.append(f"Счёт {r['amount']:,.0f}₽ по «{r['summary']}» — до {r['due_at'][:10]}{overdue}")

    # Последние платежи
    rows = conn.execute("""
        SELECT p.amount, p.paid_at, p.method, a.summary
        FROM payments p
        JOIN invoices i ON p.invoice_id = i.id
        JOIN agreements a ON i.agreement_id = a.id
        WHERE a.contact_id = ? AND p.status = 'confirmed'
        ORDER BY p.paid_at DESC LIMIT 3
    """, (contact_id,)).fetchall()
    for r in rows:
        parts.append(f"Оплата {r['amount']:,.0f}₽ ({r['method'] or '—'}) — {r['paid_at'][:10]}")

    conn.close()
    return "\n".join(parts) if parts else None


def query_overdue(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Просроченные задачи и счета."""
    conn = get_db(db_path)
    parts = []

    # Просроченные задачи
    if contact_id:
        rows = conn.execute("""
            SELECT description, due_at, priority FROM tasks
            WHERE related_type = 'contact' AND related_id = ?
              AND due_at < datetime('now') AND status NOT IN ('done', 'cancelled')
            ORDER BY due_at ASC
        """, (contact_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT description, due_at, priority FROM tasks
            WHERE due_at < datetime('now') AND status NOT IN ('done', 'cancelled')
            ORDER BY due_at ASC LIMIT 10
        """).fetchall()
    for r in rows:
        parts.append(f"📋 {r['description'][:60]} — просрочено (было {r['due_at'][:10]})")

    # Просроченные счета
    if contact_id:
        rows = conn.execute("""
            SELECT i.amount, i.due_at, a.summary
            FROM invoices i
            JOIN agreements a ON i.agreement_id = a.id
            WHERE a.contact_id = ? AND i.status = 'overdue'
        """, (contact_id,)).fetchall()
        for r in rows:
            parts.append(f"🧾 Счёт {r['amount']:,.0f}₽ — просрочен (было {r['due_at'][:10]})")

    conn.close()
    return "\n".join(parts) if parts else None


def query_tasks_today(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Задачи на сегодня."""
    conn = get_db(db_path)
    if contact_id:
        rows = conn.execute("""
            SELECT description, priority, status, due_at FROM tasks
            WHERE related_type = 'contact' AND related_id = ?
              AND date(due_at) = date('now') AND status NOT IN ('done', 'cancelled')
            ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END
        """, (contact_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT description, priority, status, due_at FROM tasks
            WHERE date(due_at) = date('now') AND status NOT IN ('done', 'cancelled')
            ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END
            LIMIT 10
        """).fetchall()
    conn.close()

    if rows:
        return "\n".join(
            f"{'🔴' if r['priority']=='urgent' else '🟡' if r['priority']=='high' else '⚪'} {r['description'][:60]}"
            for r in rows
        )
    return None


def query_leads_summary(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Сводка по активным лидам."""
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT l.amount, l.probability, l.status, c.name
        FROM leads l JOIN contacts c ON l.contact_id = c.id
        WHERE l.status NOT IN ('won', 'lost')
        ORDER BY l.created_at DESC LIMIT 5
    """).fetchall()
    conn.close()

    if rows:
        return "\n".join(
            f"• {r['name']}: {r['amount'] or '?'}₽ ({r['status']}, {(r['probability'] or 0)*100:.0f}%)"
            for r in rows
        )
    return None


def query_deal_details(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Детали сделки: сумма, стадия, сроки, заметки."""
    if not contact_id:
        return None
    conn = get_db(db_path)
    row = conn.execute("""
        SELECT d.*, c.name as contact_name FROM deals d
        JOIN contacts c ON d.contact_id = c.id
        WHERE d.contact_id = ? AND d.stage NOT IN ('closed_won', 'closed_lost')
        ORDER BY d.updated_at DESC LIMIT 1
    """, (contact_id,)).fetchone()
    conn.close()

    if row:
        amt = f"{row['amount']:,.0f}₽" if row.get("amount") else "сумма не указана"
        due = f", до {row['expected_close_at'][:10]}" if row.get("expected_close_at") else ""
        notes = f"\nЗаметки: {row['notes']}" if row.get("notes") else ""
        return f"Сделка с {row['contact_name']}: {amt}, стадия: {row['stage']}{due}{notes}"
    return None


def query_contact_history(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """История общения с контактом: ключевые memories."""
    if not contact_id:
        return None
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT content, type, importance, created_at FROM memories
        WHERE contact_id = ?
        ORDER BY importance DESC, created_at DESC LIMIT 10
    """, (contact_id,)).fetchall()
    conn.close()

    if rows:
        return "\n".join(f"• [{r['type']}] {r['content'][:80]}" for r in rows)
    return None


def query_last_interaction(contact_id, hint, chat_thread_id, db_path) -> str | None:
    """Последнее взаимодействие с контактом."""
    if not contact_id:
        # Попробуем через chat_thread
        if chat_thread_id:
            conn = get_db(db_path)
            row = conn.execute("""
                SELECT text, sent_at FROM messages
                WHERE chat_thread_id = ?
                  AND (meta_json IS NULL OR json_extract(meta_json, '$.deleted') IS NULL)
                ORDER BY sent_at DESC LIMIT 1
            """, (chat_thread_id,)).fetchone()
            conn.close()
            if row:
                return f"Последнее сообщение ({row['sent_at'][:16]}): «{row['text'][:100]}»"
        return None

    conn = get_db(db_path)
    row = conn.execute("""
        SELECT m.text, m.sent_at, ct.title
        FROM messages m
        JOIN chat_threads ct ON m.chat_thread_id = ct.id
        WHERE ct.contact_id = ?
          AND (m.meta_json IS NULL OR json_extract(m.meta_json, '$.deleted') IS NULL)
        ORDER BY m.sent_at DESC LIMIT 1
    """, (contact_id,)).fetchone()
    conn.close()

    if row:
        return f"Последнее сообщение ({row['sent_at'][:16]}): «{row['text'][:100]}»"
    return None
