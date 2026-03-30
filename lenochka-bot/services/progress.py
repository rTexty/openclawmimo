"""
Progress Check-In — owner подтверждает прогресс по задачам.

Извлечён из response_engine.py и commands.py для чистой архитектуры.

Компоненты:
1. parse_progress_reply() — LLM понимает ответ owner'а → action
2. apply_progress_update() — action → SQL UPDATE tasks
3. format_progress_confirmation() — форматирование подтверждения
4. extract_task_id_from_checkin() — извлечь task_id из check-in сообщения
5. get_task_by_id() — загрузить задачу
"""
import re
import sqlite3
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("lenochka.progress")

GMT8 = timezone(timedelta(hours=8))


# =========================================================
# 1. LLM PARSING — понимание ответа owner'а
# =========================================================

PROGRESS_REPLY_SYSTEM = """Ты — помощник владельца бизнеса в CRM-системе Lenochka.
Owner ответил на check-in сообщение о задаче. Определи что делать с задачей.

Задача: {task_description}
Дедлайн: {due_at}
Текущий статус: {status}

Ответ owner'а: {owner_reply}

Определи action (один из):
- "done" — задача выполнена
- "in_progress" — в работе, продвигается
- "extend" — нужно продлить срок (укажи new_date или extend_days)
- "blocked" — заблокирована, есть препятствие
- "cancel" — задача отменена
- "remind_tomorrow" — напомнить завтра
- "remind_date" — напомнить в конкретную дату
- "escalate" — проблема, нужно вмешательство
- "update" — просто обновление статуса, без изменения задачи

Отвечай ТОЛЬКО JSON:
{"action":"<тип>","new_date":"YYYY-MM-DD или null","extend_days":N или null,
 "notes":"<что сказал owner, кратко>","priority":"<low|normal|high|urgent или null>"}
"""


def parse_progress_reply(text: str, task: dict, brain) -> dict:
    """
    LLM понимает ответ owner'а и определяет что делать с задачей.
    Возвращает {action, new_date, extend_days, notes, priority}.
    """
    if not brain.is_ready():
        return {"action": "update", "new_date": None, "extend_days": None,
                "notes": text, "priority": None}

    try:
        result = brain._call_llm(
            PROGRESS_REPLY_SYSTEM.format(
                task_description=task.get("description", "?"),
                due_at=task.get("due_at", "не указан"),
                status=task.get("status", "open"),
                owner_reply=text,
            ),
            "", temperature=0.0, max_tokens=300,
        )
        if result:
            data = brain._extract_json(result)
            if isinstance(data, dict) and "action" in data:
                return data
    except Exception as e:
        logger.warning(f"Progress reply parse failed: {e}")

    # Fallback — записать как notes
    return {"action": "update", "new_date": None, "extend_days": None,
            "notes": text, "priority": None}


# =========================================================
# 2. SQL UPDATE — применить решение к задаче
# =========================================================

def apply_progress_update(task_id: int, decision: dict, db_path: str) -> str:
    """
    Обновить задачу на основе LLM-решения.
    Возвращает текст подтверждения для owner'а.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    action = decision.get("action", "update")
    now_note = f"[{datetime.now(GMT8).strftime('%m-%d %H:%M')}]"

    try:
        if action == "done":
            conn.execute(
                "UPDATE tasks SET status='done', updated_at=datetime('now') WHERE id=?",
                (task_id,),
            )
        elif action == "in_progress":
            conn.execute(
                "UPDATE tasks SET status='in_progress', updated_at=datetime('now') WHERE id=?",
                (task_id,),
            )
        elif action == "extend":
            if decision.get("new_date"):
                conn.execute(
                    "UPDATE tasks SET due_at=?, updated_at=datetime('now') WHERE id=?",
                    (decision["new_date"], task_id),
                )
            elif decision.get("extend_days"):
                conn.execute(
                    "UPDATE tasks SET due_at=datetime('now', ? || ' days'), updated_at=datetime('now') WHERE id=?",
                    (str(decision["extend_days"]), task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET due_at=datetime('now', '+3 days'), updated_at=datetime('now') WHERE id=?",
                    (task_id,),
                )
        elif action == "blocked":
            conn.execute(
                "UPDATE tasks SET priority='urgent', updated_at=datetime('now') WHERE id=?",
                (task_id,),
            )
        elif action == "cancel":
            conn.execute(
                "UPDATE tasks SET status='cancelled', updated_at=datetime('now') WHERE id=?",
                (task_id,),
            )

        # Notes всегда пишутся если есть
        if decision.get("notes"):
            conn.execute(
                "UPDATE tasks SET notes = COALESCE(notes || '\n', '') || ? WHERE id=?",
                (f"{now_note} {decision['notes']}", task_id),
            )

        # Priority override
        if decision.get("priority"):
            conn.execute(
                "UPDATE tasks SET priority=? WHERE id=?",
                (decision["priority"], task_id),
            )

        conn.commit()
    except Exception as e:
        logger.error(f"Progress update error: {e}")
    finally:
        conn.close()

    return format_progress_confirmation(decision)


# =========================================================
# 3. FORMATTING — подтверждение для owner'а
# =========================================================

def format_progress_confirmation(decision: dict) -> str:
    """Форматировать подтверждение обновления задачи."""
    action = decision.get("action", "update")
    notes = decision.get("notes", "")
    suffix = f" — {notes}" if notes else ""

    if action == "done":
        return f"✅ Задача закрыта{suffix}"
    elif action == "in_progress":
        return f"🔨 В работе{suffix}"
    elif action == "extend":
        if decision.get("new_date"):
            return f"📅 Дедлайн → {decision['new_date']}{suffix}"
        days = decision.get("extend_days", 3)
        return f"📅 Дедлайн продлён на {days}д{suffix}"
    elif action == "blocked":
        return f"⚠️ Заблокировано (urgent){suffix}"
    elif action == "cancel":
        return f"❌ Задача отменена{suffix}"
    elif action == "remind_tomorrow":
        return f"🔔 Напомню завтра"
    elif action == "remind_date":
        return f"🔔 Напомню {decision.get('new_date', '?')}"
    elif action == "escalate":
        return f"🚨 Эскалировано{suffix}"
    return f"📝 Записал{suffix}"


# =========================================================
# 4. CHECK-IN HELPERS — извлечение task_id, загрузка задачи
# =========================================================

TASK_ID_PATTERN = re.compile(r'\[task:(\d+)\]')


def extract_task_id_from_checkin(bot_message_text: str) -> int | None:
    """Извлечь task_id из маркера [task:ID] в check-in сообщении."""
    match = TASK_ID_PATTERN.search(bot_message_text)
    return int(match.group(1)) if match else None


def get_task_by_id(task_id: int, db_path: str) -> dict | None:
    """Загрузить задачу по ID."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
