import re

from aiogram import types, Dispatcher
from aiogram.dispatcher import DEFAULT_RATE_LIMIT
from aiogram.dispatcher.handler import CancelHandler, current_handler
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.utils.exceptions import Throttled


# Fiscal-ID pattern (same shape as utils.parse_prodleniya.FISCAL_RE).
# Kept local to avoid pulling handlers-layer imports into a middleware.
_FISCAL_RE = re.compile(r"\b[A-Z]{2}\d{10,}\b", re.IGNORECASE)


class ThrottlingMiddleware(BaseMiddleware):
    """
    Simple middleware
    """

    def __init__(self, limit=DEFAULT_RATE_LIMIT, key_prefix='antiflood_'):
        self.rate_limit = limit
        self.prefix = key_prefix
        super(ThrottlingMiddleware, self).__init__()

    async def on_process_message(self, message: types.Message, data: dict):
        # Документы пропускаем без троттлинга — массовые загрузки PDF/xlsx
        # это нормальный трафик от дилеров, флудить ими бессмысленно.
        if message.document:
            return

        # Prodleniya-текст (содержит фискалы VG/LG…) — тоже нормальный
        # bulk-трафик: юзеры реально дампят 20+ строк в тестовую группу.
        # У самого prodleniya-хендлера уже стоит per-chat asyncio.Lock,
        # так что API от параллелизма защищено; middleware только мешал,
        # блокируя сообщения ответом «Too many requests!».
        if message.text and _FISCAL_RE.search(message.text):
            return

        # Групповые чаты вообще пропускаем — там свои flow-специфичные
        # защиты (лок в prodleniya, _flood_safe для PDF-ответов).
        if message.chat and message.chat.type in ("group", "supergroup"):
            return

        handler = current_handler.get()
        dispatcher = Dispatcher.get_current()
        if handler:
            limit = getattr(handler, "throttling_rate_limit", self.rate_limit)
            key = getattr(handler, "throttling_key", f"{self.prefix}_{handler.__name__}")
        else:
            limit = self.rate_limit
            key = f"{self.prefix}_message"
        try:
            await dispatcher.throttle(key, rate=limit)
        except Throttled as t:
            await self.message_throttled(message, t)
            raise CancelHandler()

    async def message_throttled(self, message: types.Message, throttled: Throttled):
        if throttled.exceeded_count <= 20:
            await message.reply("Too many requests!")
