"""
Command Handlers — /start, /status, /leads, /tasks, /digest, /weekly, /find, /help

Все команды защищены is_owner (инжектируется OwnerMiddleware).
Non-owner получает приветствие и блокировку.
"""
import re
import sqlite3
import logging
from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.enums import ParseMode

from config import settings
from services import memory as mem

router = Router(name="commands")
logger = logging.getLogger("lenochka.commands")


async def _check_owner(message: Message, is_owner: bool) -> bool:
    """Проверить владение. Возвращает True если можно продолжать."""
    if is_owner:
        return True
    await message.answer("👋 Привет! Я Lenochka — AI-ассистент. Пока я работаю только для своего владельца.")
    return False


@router.message(Command("start"))
async def cmd_start(message: Message, is_owner: bool = False, **kwargs):
    if not await _check_owner(message, is_owner):
        return

    status = mem.get_business_status(message.from_user.id, settings.db_path)

    text = (
        "🤖 <b>Lenochka</b> — ваш AI-ассистент\n\n"
        "Анализирую переписки, извлекаю задачи, слежу за лидами.\n\n"
    )

    if status.get("connected"):
        text += "✅ Business-аккаунт подключён.\n"
    else:
        text += (
            "⚠️ <b>Подключите бота к Business-аккаунту:</b>\n"
            "1. Настройки → Telegram Business\n"
            "2. Боты → Добавить бота\n"
            "3. Включите «Чтение сообщений»\n"
        )

    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("status"))
async def cmd_status(message: Message, brain, is_owner: bool = False, **kwargs):
    if not await _check_owner(message, is_owner):
        return

    s = mem.get_status_summary(settings.db_path)
    text = (
        f"📊 <b>Статус</b>\n\n"
        f"📨 Сегодня: {s['messages_today']} сообщений\n"
        f"🔥 Лидов: {s['active_leads']}\n"
        f"💰 Сделок: {s['open_deals']}\n"
        f"📋 Задач: {s['open_tasks']}\n"
        f"⚠️ Просрочено: {s['overdue_tasks']}\n"
        f"🧠 Memories: {s['total_memories']}\n"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("leads"))
async def cmd_leads(message: Message, is_owner: bool = False, **kwargs):
    if not await _check_owner(message, is_owner):
        return

    leads = mem.get_active_leads(settings.db_path)
    if not leads:
        await message.answer("🔥 Нет активных лидов.")
        return

    lines = []
    for l in leads:
        amount = f" — {l['amount']:,.0f}₽" if l.get("amount") else ""
        lines.append(f"• <b>{l['contact_name']}</b>{amount} [{l['status']}]")

    await message.answer(
        f"🔥 <b>Лиды ({len(leads)}):</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("tasks"))
async def cmd_tasks(message: Message, is_owner: bool = False, **kwargs):
    if not await _check_owner(message, is_owner):
        return

    tasks = mem.get_open_tasks(settings.db_path)
    if not tasks:
        await message.answer("📋 Нет открытых задач.")
        return

    lines = []
    for t in tasks:
        due = f" (до {t['due_at'][:10]})" if t.get("due_at") else ""
        icon = "🔴" if t["priority"] == "urgent" else "🟡" if t["priority"] == "high" else "⚪"
        lines.append(f"{icon} {t['description'][:60]}{due}")

    await message.answer(
        f"📋 <b>Задачи ({len(tasks)}):</b>\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("digest"))
async def cmd_digest(message: Message, brain, is_owner: bool = False, **kwargs):
    if not await _check_owner(message, is_owner):
        return

    if brain.is_ready():
        text = brain.daily_digest()
    else:
        text = "⚠️ Brain не инициализирован."
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("weekly"))
async def cmd_weekly(message: Message, brain, is_owner: bool = False, **kwargs):
    if not await _check_owner(message, is_owner):
        return

    if brain.is_ready():
        text = brain.weekly_digest()
    else:
        text = "⚠️ Brain не инициализирован."
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("find"))
async def cmd_find(message: Message, brain, is_owner: bool = False, **kwargs):
    if not await _check_owner(message, is_owner):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /find <запрос>")
        return

    results = mem.search_memory(parts[1], settings.db_path, brain)
    if not results:
        await message.answer("Ничего не найдено.")
        return

    # Основные результаты
    lines = []
    entity_block = ""
    for r in results:
        if r.get("source") == "entity_expansion" and r.get("_expansion"):
            # Entity context — форматируем отдельным блоком
            entity_block = mem.format_expansion_for_tg(r["_expansion"])
            continue
        lines.append(f"• [{r.get('type', '?')}] {r['content'][:80]}")

    text = "🔍 <b>Результаты:</b>\n\n" + "\n".join(lines)
    if entity_block:
        text += "\n\n" + entity_block

    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("help"))
async def cmd_help(message: Message, is_owner: bool = False, **kwargs):
    if not await _check_owner(message, is_owner):
        return

    text = (
        "🤖 <b>Lenochka — команды</b>\n\n"
        "/status — статус CRM\n"
        "/leads — активные лиды\n"
        "/tasks — открытые задачи\n"
        "/digest — дайджест за сегодня\n"
        "/weekly — недельный отчёт\n"
        "/find <запрос> — поиск по памяти\n"
        "/help — помощь\n\n"
        "💡 Просто пишите в Telegram — я автоматически "
        "анализирую переписки и веду CRM."
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


# Direct non-command messages — ingest too
@router.message(F.chat.type == "private")
async def on_direct_message(message: Message, pipeline, brain=None,
                            is_owner: bool = False, **kwargs):
    """
    Прямые сообщения боту (не команды).

    Если owner ответил reply на check-in сообщение → обработать прогресс.
    Если owner написал не-команду → подсказка.
    Если не owner → приветствие.
    """
    if not is_owner:
        await message.answer("👋 Привет! Я Lenochka — AI-ассистент. Пока я работаю только для своего владельца.")
        return

    # Check: это ответ на progress check-in?
    if message.reply_to_message:
        task_id = _extract_task_id_from_checkin(
            message.reply_to_message.text or ""
        )
        if task_id:
            task = _get_task_by_id(task_id, settings.db_path)
            if task and brain and brain.is_ready():
                from services.response_engine import (
                    parse_progress_reply, format_progress_confirmation,
                )
                decision = parse_progress_reply(message.text, task, brain)
                result = _apply_progress_update(task_id, decision, settings.db_path)
                if result:
                    await message.answer(result)
                return
            elif task:
                await message.answer("⚠️ Brain не готов. Записал как заметку.")
                _apply_progress_update(task_id, {"action": "update", "notes": message.text}, settings.db_path)
                return

    # Owner написал не-команду в личку боту
    await message.answer(
        "💡 Пиши команды: /status, /leads, /tasks, /digest, /find <запрос>\n"
        "Или просто живи в Telegram — я анализирую переписки автоматически."
    )


# =========================================================
# PROGRESS CHECK-IN HELPERS
# =========================================================

def _extract_task_id_from_checkin(bot_message_text: str) -> int | None:
    """Извлечь task_id из маркера [task:ID]."""
    match = re.search(r'\[task:(\d+)\]', bot_message_text)
    return int(match.group(1)) if match else None


def _get_task_by_id(task_id: int, db_path: str) -> dict | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _apply_progress_update(task_id: int, decision: dict, db_path: str) -> str:
    """Обновить задачу на основе LLM-решения. Возвращает текст подтверждения."""
    from services.response_engine import format_progress_confirmation
    from datetime import datetime, timedelta, timezone

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    action = decision.get("action", "update")
    now_note = f"[{datetime.now(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')}]"

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

        if decision.get("notes"):
            conn.execute(
                "UPDATE tasks SET notes = COALESCE(notes || '\n', '') || ? WHERE id=?",
                (f"{now_note} {decision['notes']}", task_id),
            )

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
