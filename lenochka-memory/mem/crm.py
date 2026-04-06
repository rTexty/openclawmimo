import sys
from datetime import datetime

from mem._db import get_db


def crm_contact(tg=None, contact_id=None):
    """Получить контакт."""
    conn = get_db()
    if contact_id:
        row = conn.execute(
            "SELECT * FROM contacts WHERE id = ?", (contact_id,)
        ).fetchone()
    elif tg:
        tg_clean = tg.lstrip("@")
        row = conn.execute(
            "SELECT * FROM contacts WHERE tg_username = ?", (tg_clean,)
        ).fetchone()
    else:
        print("Укажите --tg или --contact-id")
        return None
    conn.close()
    return dict(row) if row else None


def crm_deals(contact_id=None):
    """Сделки контакта."""
    conn = get_db()
    if contact_id:
        rows = conn.execute(
            """
            SELECT d.*, c.name as contact_name FROM deals d
            JOIN contacts c ON d.contact_id = c.id
            WHERE d.contact_id = ?
            ORDER BY d.created_at DESC
        """,
            (contact_id,),
        ).fetchall()
    else:
        rows = conn.execute("""
            SELECT d.*, c.name as contact_name FROM deals d
            JOIN contacts c ON d.contact_id = c.id
            WHERE d.stage NOT IN ('closed_won', 'closed_lost')
            ORDER BY d.expected_close_at ASC
        """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def crm_overdue_tasks():
    """Просроченные задачи."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM v_overdue_tasks").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def crm_abandoned(hours=24):
    """Брошенные диалоги."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT ct.id, ct.title, ct.tg_chat_id,
               c.name as contact_name, c.tg_username,
               MAX(m.sent_at) as last_message_at,
               (julianday('now') - julianday(MAX(m.sent_at))) * 24 as hours_since
        FROM chat_threads ct
        JOIN messages m ON m.chat_thread_id = ct.id
        LEFT JOIN contacts c ON ct.contact_id = c.id
        WHERE m.from_user_id != 'self'
        GROUP BY ct.id
        HAVING hours_since > ?
        ORDER BY hours_since DESC
    """,
        (hours,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def crm_leads(since=None):
    """Лиды."""
    conn = get_db()
    if since:
        rows = conn.execute(
            """
            SELECT l.*, c.name as contact_name FROM leads l
            JOIN contacts c ON l.contact_id = c.id
            WHERE l.created_at >= ?
            ORDER BY l.created_at DESC
        """,
            (since,),
        ).fetchall()
    else:
        rows = conn.execute("""
            SELECT l.*, c.name as contact_name FROM leads l
            JOIN contacts c ON l.contact_id = c.id
            WHERE l.status NOT IN ('won', 'lost')
            ORDER BY l.created_at DESC
        """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def crm_daily_summary(date=None):
    """Сводка за день."""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    start = f"{date} 00:00:00"
    end = f"{date} 23:59:59"

    result = {
        "date": date,
        "messages": conn.execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE sent_at BETWEEN ? AND ?",
            (start, end),
        ).fetchone()["cnt"],
        "new_leads": conn.execute(
            "SELECT COUNT(*) as cnt FROM leads WHERE created_at BETWEEN ? AND ?",
            (start, end),
        ).fetchone()["cnt"],
        "new_tasks": conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE created_at BETWEEN ? AND ?",
            (start, end),
        ).fetchone()["cnt"],
        "completed_tasks": conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE status = 'done' AND updated_at BETWEEN ? AND ?",
            (start, end),
        ).fetchone()["cnt"],
        "memories": conn.execute(
            "SELECT COUNT(*) as cnt FROM memories WHERE created_at BETWEEN ? AND ?",
            (start, end),
        ).fetchone()["cnt"],
    }
    conn.close()
    return result
