"""Logging middleware — structured log для каждого update."""
import logging
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

logger = logging.getLogger("lenochka.incoming")


class LoggingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        # Log incoming event type
        event_type = type(event).__name__
        chat_id = None
        user_id = None

        if hasattr(event, "chat") and event.chat:
            chat_id = event.chat.id
        if hasattr(event, "from_user") and event.from_user:
            user_id = event.from_user.id

        extra = f"chat={chat_id} user={user_id}"

        if hasattr(event, "text") and event.text:
            text = event.text[:80]
            logger.debug(f"→ {event_type} | {extra} | {text}")
        elif hasattr(event, "business_connection_id") and event.business_connection_id:
            logger.debug(f"→ {event_type} | {extra} | biz_conn={event.business_connection_id}")
        else:
            logger.debug(f"→ {event_type} | {extra}")

        return await handler(event, data)
