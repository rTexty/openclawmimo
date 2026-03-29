"""Middleware setup."""
from aiogram import Dispatcher
from middlewares.throttling import ThrottlingMiddleware
from middlewares.logging import LoggingMiddleware
from config import settings


def setup_middlewares(dp: Dispatcher, brain):
    """Регистрация middleware. Порядок: Logging → Throttling."""
    dp.message.middleware(LoggingMiddleware())
    dp.message.middleware(ThrottlingMiddleware(rate_limit=settings.rate_limit_messages))

    # Inject brain и pipeline в handler data
    dp.workflow_data.update(brain=brain)
