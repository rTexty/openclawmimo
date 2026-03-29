"""
Owner Authentication Middleware.

Проверяет что сообщение от владельца бота (settings.owner_id).
Для business_messages — всегда пропускает (owner определяется через business_connection).
Для direct messages — проверяет message.from_user.id == owner_id.

Инжектирует is_owner=True/False в handler data.
"""
import logging
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from config import settings

logger = logging.getLogger("lenochka.auth")


class OwnerMiddleware(BaseMiddleware):
    """
    Middleware для проверки владельца.
    
    Логика:
    - Business messages: всегда owner (business_connection_id = принадлежность аккаунту)
    - Direct messages: проверяем from_user.id == settings.owner_id
    - Если owner_id не настроен (0): пропускаем всех (dev mode)
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        # Business messages — всегда owner (привязка через business_connection)
        if event.business_connection_id is not None:
            data["is_owner"] = True
            return await handler(event, data)

        # Owner ID не настроен — dev mode, пропускаем всех
        if not settings.owner_id:
            data["is_owner"] = True
            return await handler(event, data)

        # Direct message — проверяем владельца
        user_id = event.from_user.id if event.from_user else None
        is_owner = user_id == settings.owner_id
        data["is_owner"] = is_owner

        return await handler(event, data)
