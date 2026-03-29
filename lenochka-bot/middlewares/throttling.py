"""Throttling middleware — anti-spam."""
import time
from collections import defaultdict
from aiogram import BaseMiddleware
from aiogram.types import Message


class ThrottlingMiddleware(BaseMiddleware):
    """Не более N сообщений в минуту от одного пользователя."""

    def __init__(self, rate_limit: int = 30):
        self.rate_limit = rate_limit
        self.history: dict[int, list[float]] = defaultdict(list)
        self._last_cleanup = time.time()

    async def __call__(self, handler, event: Message, data: dict):
        user_id = event.from_user.id if event.from_user else 0
        now = time.time()

        # Cleanup every 5 min
        if now - self._last_cleanup > 300:
            cutoff = now - 60
            for uid in list(self.history.keys()):
                self.history[uid] = [t for t in self.history[uid] if t > cutoff]
                if not self.history[uid]:
                    del self.history[uid]
            self._last_cleanup = now

        # Check rate
        self.history[user_id] = [
            t for t in self.history[user_id] if now - t < 60
        ]
        if len(self.history[user_id]) >= self.rate_limit:
            return  # Silently drop

        self.history[user_id].append(now)
        return await handler(event, data)
