"""
Command Handlers — /start, /status, /leads, /tasks, /digest, /weekly, /find, /help

Все команды защищены is_owner (инжектируется OwnerMiddleware).
Non-owner получает приветствие и блокировку.
"""
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
async def on_direct_message(message: Message, pipeline, is_owner: bool = False, **kwargs):
    """
    Прямые сообщения боту (не команды).
    
    Если owner — НЕ пишем в CRM pipeline (это не переписка с клиентом,
    а управление ботом). Вместо этого — подсказка или обработка в будущем
    (response engine).
    
    Если не owner — приветствие.
    """
    if not is_owner:
        await message.answer("👋 Привет! Я Lenochka — AI-ассистент. Пока я работаю только для своего владельца.")
        return

    # Owner написал не-команду в личку боту
    # НЕ ingest в pipeline — это засоряет CRM данными, которые не являются перепиской с клиентом
    # В будущем здесь будет response engine (LLM-ответ от имени бота)
    await message.answer(
        "💡 Пиши команды: /status, /leads, /tasks, /digest, /find <запрос>\n"
        "Или просто живи в Telegram — я анализирую переписки автоматически."
    )
