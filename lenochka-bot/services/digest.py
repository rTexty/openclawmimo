"""Digest service — генерация и форматирование дайджестов."""
import logging
from aiogram import Bot
from aiogram.enums import ParseMode
from config import settings

logger = logging.getLogger("lenochka.digest")


async def generate_and_send_daily(bot: Bot, brain):
    """Сгенерировать и отправить дайджест за сегодня."""
    if not brain.is_ready():
        logger.warning("Brain not ready for digest")
        return

    text = brain.daily_digest()
    await bot.send_message(
        chat_id=settings.owner_id,
        text=text,
        parse_mode=ParseMode.HTML,
    )
    logger.info("Daily digest sent")


async def generate_and_send_weekly(bot: Bot, brain):
    """Сгенерировать и отправить недельный отчёт."""
    if not brain.is_ready():
        return

    text = brain.weekly_digest()
    await bot.send_message(
        chat_id=settings.owner_id,
        text=text,
        parse_mode=ParseMode.HTML,
    )
    logger.info("Weekly report sent")


async def check_abandoned(bot: Bot, brain):
    """Проверить брошенные диалоги и уведомить."""
    from services import memory as mem

    abandoned = mem.get_abandoned_dialogues(48, settings.db_path)
    if not abandoned:
        return

    lines = [
        f"• {d.get('contact_name') or d.get('title', '?')}: "
        f"{int(d.get('hours_since', 0))}ч без ответа"
        for d in abandoned[:5]
    ]

    text = "👻 <b>Брошенные диалоги:</b>\n\n" + "\n".join(lines)
    await bot.send_message(
        chat_id=settings.owner_id,
        text=text,
        parse_mode=ParseMode.HTML,
    )
