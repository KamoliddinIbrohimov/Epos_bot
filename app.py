import logging

import asyncpg
from aiogram import executor

from data import config
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
    except asyncpg.InvalidPasswordError as e:
        logging.error(
            "DB auth failed: DB_PASS из .env не совпадает с паролем, "
            "сохранённым в volume pgdata "
            "(user=%s, db=%s, host=%s). Варианты починки:\n"
            "  A) Снести volume и переинициализировать (данные будут утеряны):\n"
            "       docker compose down -v && docker compose up -d --build\n"
            "  B) Поменять пароль внутри Postgres под текущий .env:\n"
            "       docker exec -it epos_db psql -U %s\n"
            "       ALTER USER %s WITH PASSWORD '<значение DB_PASS из .env>';",
            config.DB_USER, config.DB_NAME, config.DB_HOST,
            config.DB_USER, config.DB_USER,
        )
        raise
    except Exception as e:
        logging.exception("on_startup failed")
        try:
            await notify_admins_error("Ошибка при запуске бота", exc=e)
        except Exception:
            pass
        raise


if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup)
