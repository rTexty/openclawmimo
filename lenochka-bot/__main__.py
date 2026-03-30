"""
Lenochka Telegram Bot — Entry Point.

Поддерживает два режима:
  - Polling (default): long-polling, для разработки и VPS без домена
  - Webhook: для продакшена с HTTPS-доменом

Режим определяется наличием LEN_WEBHOOK_URL в переменных окружения.
"""
import asyncio
import logging
import signal

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from aiohttp import web

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
        bot=bot,
        batch_size=settings.pipeline_batch_size,
        batch_interval=settings.pipeline_batch_interval,
    )
    dp["pipeline"] = pipeline

    # 6. Scheduler
    scheduler = create_scheduler(bot, brain)

    # 7. Register bot commands
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

    allowed_updates = [
        "message", "edited_message",
        "business_connection", "business_message",
        "edited_business_message", "deleted_business_messages",
        "callback_query", "my_chat_member",
    ]

    # 8. Choose mode: webhook or polling
    if settings.webhook_url:
        await _run_webhook(bot, dp, brain, pipeline, scheduler, allowed_updates)
    else:
        await _run_polling(bot, dp, pipeline, scheduler, allowed_updates)


async def _run_polling(bot, dp, pipeline, scheduler, allowed_updates):
    """Long-polling mode — для разработки и VPS без домена."""
    async def on_startup(**kwargs):
        await pipeline.start()
        scheduler.start()

        # Startup recovery: восстановить pending notifications
        from services.notifier import recover_pending_notifications
        await recover_pending_notifications(bot, settings.db_path)

        logger.info("Lenochka started (polling) ✓")

    async def on_shutdown(**kwargs):
        await pipeline.stop()
        scheduler.shutdown(wait=False)
        logger.info("Lenochka stopped")

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logger.info("Starting polling...")
    await dp.start_polling(bot, allowed_updates=allowed_updates)


async def _run_webhook(bot, dp, brain, pipeline, scheduler, allowed_updates):
    """
    Webhook mode — для продакшена с HTTPS.
    
    Архитектура:
    - aiohttp web.Application слушает webhook_port
    - POST /webhook/{token} → обрабатывает update
    - GET /health → healthcheck для мониторинга
    
    На старте:
    1. Устанавливаем webhook в Telegram API
    2. Запускаем aiohttp server
    3. Запускаем pipeline + scheduler
    
    На shutdown:
    1. Удаляем webhook
    2. Останавливаем pipeline + scheduler
    """
    webhook_path = f"/webhook/{settings.bot_token}"
    webhook_url = f"{settings.webhook_url.rstrip('/')}{webhook_path}"

    # Setup aiohttp app
    app = web.Application()

    async def handle_webhook(request: web.Request) -> web.Response:
        """Обработчик входящих updates от Telegram."""
        if request.match_info.get("token") != settings.bot_token:
            return web.Response(status=403)

        update_data = await request.json()
        from aiogram.types import Update
        update = Update(**update_data)

        # Feed update into dispatcher (не блокирует event loop)
        asyncio.create_task(
            dp.feed_update(bot, update)
        )
        return web.Response(text="OK")

    async def handle_health(request: web.Request) -> web.Response:
        """Healthcheck endpoint."""
        status = "ok" if brain.is_ready() else "loading"
        return web.json_response({"status": status, "mode": "webhook"})

    app.router.add_post(webhook_path, handle_webhook)
    app.router.add_get("/health", handle_health)

    # Startup sequence
    async def on_startup(app_instance):
        await pipeline.start()
        scheduler.start()

        # Startup recovery: восстановить pending notifications
        from services.notifier import recover_pending_notifications
        await recover_pending_notifications(bot, settings.db_path)

        # Устанавливаем webhook в Telegram
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url != webhook_url:
            await bot.set_webhook(
                url=webhook_url,
                allowed_updates=allowed_updates,
                secret_token=settings.webhook_secret or None,
            )
            logger.info(f"Webhook set: {webhook_url}")
        else:
            logger.info(f"Webhook already set: {webhook_url}")

        logger.info("Lenochka started (webhook) ✓")

    async def on_shutdown(app_instance):
        await bot.delete_webhook()
        await pipeline.stop()
        scheduler.shutdown(wait=False)
        await bot.session.close()
        logger.info("Lenochka stopped (webhook)")

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # Graceful shutdown on signals
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(app.shutdown()))

    # Start server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=settings.webhook_port)
    await site.start()
    logger.info(f"Webhook server listening on 0.0.0.0:{settings.webhook_port}")

    # Keep running
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
