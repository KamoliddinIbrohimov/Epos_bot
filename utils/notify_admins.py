import logging

from aiogram import Dispatcher

from data.config import ADMINS


async def on_startup_notify(dp: Dispatcher):
    for admin in ADMINS:
        try:
            await dp.bot.send_message(admin, "The bot has started")

        except Exception as err:
            logging.exception(err)


async def notify_admins(text: str) -> None:
    """Send a plain HTML message to every ADMIN. Per-admin failures are
    logged but never propagate."""
    from loader import bot  # lazy — избегаем circular import через utils/__init__

    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            logging.warning("notify_admins: failed to notify %s: %s", admin_id, e)
