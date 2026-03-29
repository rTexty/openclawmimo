"""Global error handler."""
import logging
from aiogram import Router
from aiogram.types import ErrorEvent

router = Router(name="errors")
logger = logging.getLogger("lenochka.errors")


@router.error()
async def on_error(event: ErrorEvent):
    logger.error(f"Handler error: {event.exception}", exc_info=event.exception)
