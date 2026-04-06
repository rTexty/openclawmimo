"""Генерация дайджестов: ежедневный и еженедельный."""

from datetime import timedelta

from brain._config import _now_gmt8
from brain._db import _get_db


def generate_daily_digest(date=None):
    """Сгенерировать утренний дайджест."""
    if date is None:
        date = _now_gmt8().strftime("%Y-%m-%d")

    conn = _get_db()
    start = f"{date} 00:00:00"
    end = f"{date} 23:59:59"
    sections = []

    # 1. Новые лиды
    leads = conn.execute(
        """
        SELECT l.*, c.name as contact_name FROM leads l
        JOIN contacts c ON l.contact_id = c.id
        WHERE l.created_at BETWEEN ? AND ?
    """,
        (start, end),
    ).fetchall()
    if leads:
        lines = [
            f"• {l['contact_name']}: {l.get('source', '?')}, {l.get('amount', '?')} руб, статус: {l['status']}"
            for l in leads
        ]
        sections.append(f"🔥 Новые лиды ({len(leads)}):\n" + "\n".join(lines))

    # 2. Просроченные задачи
    overdue = conn.execute("SELECT * FROM v_overdue_tasks").fetchall()
    if overdue:
        lines = [f"• {t['description'][:60]} (просрочено)" for t in overdue]
        sections.append(f"⚠️ Просроченные задачи ({len(overdue)}):\n" + "\n".join(lines))

    # 3. Брошенные диалоги
    abandoned = conn.execute("""
        SELECT ct.title, c.name, MAX(m.sent_at) as last_at,
               (julianday('now') - julianday(MAX(m.sent_at))) * 24 as hours
        FROM chat_threads ct
        JOIN messages m ON m.chat_thread_id = ct.id
        LEFT JOIN contacts c ON ct.contact_id = c.id
        WHERE m.from_user_id != 'self'
        GROUP BY ct.id
        HAVING hours > 24
        ORDER BY hours DESC LIMIT 10
    """).fetchall()
    if abandoned:
        lines = [
            f"• {a['name'] or a['title']}: {int(a['hours'])}ч без ответа"
            for a in abandoned
        ]
        sections.append(
            f"👻 Брошенные диалоги ({len(abandoned)}):\n" + "\n".join(lines)
        )

    # 4. Ключевые события дня
    events = conn.execute(
        """
        SELECT content, type, importance FROM memories
        WHERE created_at BETWEEN ? AND ? AND importance >= 0.7
        ORDER BY importance DESC LIMIT 5
    """,
        (start, end),
    ).fetchall()
    if events:
        lines = [f"• [{e['type']}] {e['content'][:80]}" for e in events]
        sections.append(f"📌 Ключевые события:\n" + "\n".join(lines))

    conn.close()

    if not sections:
        return f"📅 Дайджест за {date}\n\nТихий день — ничего важного."
    return f"📅 Дайджест за {date}\n\n" + "\n\n".join(sections)


def generate_weekly_digest(weeks_back=0):
    """Сгенерировать недельный дайджест."""
    now = _now_gmt8()
    end_date = now - timedelta(weeks=weeks_back)
    start_date = end_date - timedelta(days=7)
    start = start_date.strftime("%Y-%m-%d")
    end = end_date.strftime("%Y-%m-%d")

    conn = _get_db()
    stats = {}
    for key, query in [
        (
            "messages",
            "SELECT COUNT(*) as c FROM messages WHERE sent_at BETWEEN ? AND ?",
        ),
        (
            "new_leads",
            "SELECT COUNT(*) as c FROM leads WHERE created_at BETWEEN ? AND ?",
        ),
        (
            "new_tasks",
            "SELECT COUNT(*) as c FROM tasks WHERE created_at BETWEEN ? AND ?",
        ),
        (
            "completed_tasks",
            "SELECT COUNT(*) as c FROM tasks WHERE status='done' AND updated_at BETWEEN ? AND ?",
        ),
        (
            "memories",
            "SELECT COUNT(*) as c FROM memories WHERE created_at BETWEEN ? AND ?",
        ),
    ]:
        stats[key] = conn.execute(
            query, (f"{start} 00:00:00", f"{end} 23:59:59")
        ).fetchone()["c"]
    conn.close()

    conv = round(stats["completed_tasks"] / max(stats["new_tasks"], 1) * 100)
    return f"""📊 Недельный дайджест ({start} — {end})

📨 Сообщений: {stats["messages"]}
🔥 Новых лидов: {stats["new_leads"]}
📋 Новых задач: {stats["new_tasks"]}
✅ Завершённых задач: {stats["completed_tasks"]}
🧠 Записей в памяти: {stats["memories"]}

Конверсия задач: {stats["completed_tasks"]}/{stats["new_tasks"]} ({conv}%)"""
