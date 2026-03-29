"""Business message filter."""
from aiogram.filters import Filter
from aiogram.types import Message


class IsBusinessMessage(Filter):
    """Фильтр: сообщение из business_connection."""
    async def __call__(self, message: Message) -> bool:
        return bool(message.business_connection_id)
