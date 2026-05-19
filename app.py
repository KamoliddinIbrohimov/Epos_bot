import logging

from aiogram import executor

from loader import dp, db
import middlewares, filters, handlers
from utils.notify_admins import on_startup_notify
from utils.notify_errors import notify_admins_error
from utils.set_bot_commands import set_default_commands


async def on_startup(dispatcher):
    try:
        await db.create()
        await db.create_table_users()
        await db.create_table_settings()
        await db.create_table_chats()
        await db.create_table_dillers()
        await db.create_table_diller_chats()

        await set_default_commands(dispatcher)
        await on_startup_notify(dispatcher)
    except Exception as e:
        logging.exception("on_startup failed")
        try:
            await notify_admins_error("Ошибка при запуске бота", exc=e)
        except Exception:
            pass
        raise


if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup)
