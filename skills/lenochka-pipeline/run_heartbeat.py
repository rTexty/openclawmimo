#!/usr/bin/env python3
"""
Lenochka Heartbeat — периодические проверки состояния.

Проверяет:
1. Просроченные pending_notifications
2. Просроченные задачи (v_overdue_tasks)
3. Брошенные диалоги (>48ч без ответа)

Вывод: stdout = текст уведомления для Камиля (пустой = тишина).

Использование:
  python3 run_heartbeat.py [--db-path /path/to/lenochka.db]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

NIGHT_HOUR_START = 23
NIGHT_HOUR_END = 8

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MEMORY_DIR = PROJECT_ROOT / "lenochka-memory"
sys.path.insert(0, str(MEMORY_DIR))

from config import TZ_OWNER  # noqa: E402


def log(msg: str) -> None:
    print(f"[heartbeat] {msg}", file=sys.stderr)


def is_night_for_owner() -> bool:
    h = datetime.now(TZ_OWNER).hour
    return h >= NIGHT_HOUR_START or h < NIGHT_HOUR_END


def get_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def check_pending_notifications(conn: sqlite3.Connection) -> list[str]:
    """Найти просроченные эскалации."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT pn.id, pn.escalation_type, c.name, m.text "
        "FROM pending_notifications pn "
        "JOIN contacts c ON pn.contact_id = c.id "
        "JOIN messages m ON pn.message_id = m.id "
        "WHERE pn.status = 'pending' AND pn.notify_at <= ? "
        "ORDER BY pn.notify_at ASC LIMIT 5",
        (now,),
    ).fetchall()

    if not rows:
        return []

    lines = [f"⏰ Просроченные уведомления ({len(rows)}):"]
    for r in rows:
        icon = "🚨" if r["escalation_type"] in ("risk", "complaint") else "🔔"
        lines.append(f"{icon} {r['name']}: {r['text'][:80]}")
        # Пометить как обработанное
        conn.execute(
            "UPDATE pending_notifications SET status='notified' WHERE id=?",
            (r["id"],),
        )
    conn.commit()
    return lines


def check_overdue_tasks(conn: sqlite3.Connection) -> list[str]:
    """Найти просроченные задачи."""
    rows = conn.execute(
        "SELECT t.description, t.due_at, c.name "
        "FROM tasks t "
        "LEFT JOIN contacts c ON t.related_type='contact' AND t.related_id=c.id "
        "WHERE t.due_at < datetime('now') "
        "AND t.status NOT IN ('done','cancelled') "
        "ORDER BY t.due_at ASC LIMIT 5",
    ).fetchall()

    if not rows:
        return []

    lines = [f"⚠️ Просроченные задачи ({len(rows)}):"]
    for r in rows:
        who = f" ({r['name']})" if r["name"] else ""
        lines.append(f"• {r['description']}{who} — до {r['due_at'][:10]}")
    return lines


def check_abandoned_dialogues(conn: sqlite3.Connection) -> list[str]:
    """Найти диалоги без ответа > 10 минут (клиент написал, Камиль не ответил)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = conn.execute(
        "SELECT ct.id as chat_thread_id, ct.title, ct.tg_chat_id, "
        "c.name, c.tg_username, MAX(m.sent_at) as last_msg "
        "FROM chat_threads ct "
        "JOIN contacts c ON ct.contact_id = c.id "
        "JOIN messages m ON m.chat_thread_id = ct.id "
        "WHERE m.sent_at < ? "
        "AND m.from_user_id != 'self' "
        "AND m.analyzed = 1 "
        "AND m.classification NOT IN ('noise', 'chit-chat', 'owner_message') "
        "GROUP BY ct.id "
        "HAVING COUNT(CASE WHEN m.analyzed = 0 THEN 1 END) = 0 "
        "ORDER BY last_msg DESC LIMIT 5",
        (cutoff,),
    ).fetchall()

    if not rows:
        return []

    lines = [f"👻 Брошенные диалоги ({len(rows)}):"]
    for r in rows:
        who = r["name"] or r["title"] or "Unknown"
        if r["tg_username"]:
            who += f" (@{r['tg_username']})"
        lines.append(f"• {who} — последнее сообщение {r['last_msg'][:16]}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="Lenochka Heartbeat")
    parser.add_argument("--db-path", default=None, help="Путь к SQLite БД")
    args = parser.parse_args()

    db_path = args.db_path or str(MEMORY_DIR / "db" / "lenochka.db")

    # Ночью не шумим
    if is_night_for_owner():
        log("night mode → silent")
        return

    conn = get_db(db_path)
    try:
        sections = []
        sections.extend(check_pending_notifications(conn))
        sections.extend(check_overdue_tasks(conn))
        sections.extend(check_abandoned_dialogues(conn))
    finally:
        conn.close()

    if sections:
        print("\n".join(sections))
    # Пустой stdout = тишина


if __name__ == "__main__":
    main()
