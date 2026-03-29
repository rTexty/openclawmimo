"""
Lenochka Telegram Bot — Entry Point.
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from config import settings
from services.brain_wrapper import BrainWrapper
from services.pipeline import PipelineProcessor
from services.scheduler import create_scheduler
from handlers import setup_routers
from middlewares import setup_middlewares

logger = logging.getLogger("lenochka")


def create_bot() -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dp() -> Dispatcher:
    return Dispatcher()


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    )

    # 1. Brain — модель загружается ОДИН раз
    brain = BrainWrapper()
    await brain.initialize()

    # 2. Bot + Dispatcher
    bot = create_bot()
    dp = create_dp()

    # 3. Routers
    dp.include_router(setup_routers())

    # 4. Middleware
    setup_middlewares(dp, brain)

    # 5. Pipeline (async queue)
    pipeline = PipelineProcessor(
        brain=brain,
        db_path=settings.db_path,
        batch_size=settings.pipeline_batch_size,
        batch_interval=settings.pipeline_batch_interval,
    )
    dp["pipeline"] = pipeline

    # 6. Scheduler
    scheduler = create_scheduler(bot, brain)

    # 7. Startup / Shutdown hooks
    async def on_startup(**kwargs):
        await pipeline.start()
        scheduler.start()
        logger.info("Lenochka started ✓")

    async def on_shutdown(**kwargs):
        await pipeline.stop()
        scheduler.shutdown(wait=False)
        logger.info("Lenochka stopped")

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # 8. Register bot commands
    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск"),
        BotCommand(command="status", description="Статус"),
        BotCommand(command="leads", description="Лиды"),
        BotCommand(command="tasks", description="Задачи"),
        BotCommand(command="digest", description="Дайджест"),
        BotCommand(command="weekly", description="Неделя"),
        BotCommand(command="find", description="Поиск"),
        BotCommand(command="help", description="Помощь"),
    ])

    # 9. Start polling
    logger.info("Starting polling...")
    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "edited_message",
            "business_connection",
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
            "callback_query",
            "my_chat_member",
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
