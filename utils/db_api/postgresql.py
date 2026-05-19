import asyncpg
from asyncpg import Connection
from asyncpg.pool import Pool

from data import config


class Database:
    def __init__(self):
        self.pool: Pool = None

    async def create(self):
        self.pool = await asyncpg.create_pool(
            user=config.DB_USER,
            password=config.DB_PASS,
            host=config.DB_HOST,
            database=config.DB_NAME,
        )

    async def execute(
        self,
        command,
        *args,
        fetch: bool = False,
        fetchval: bool = False,
        fetchrow: bool = False,
        execute: bool = False,
    ):
        async with self.pool.acquire() as connection:
            connection: Connection
            async with connection.transaction():
                if fetch:
                    result = await connection.fetch(command, *args)
                elif fetchval:
                    result = await connection.fetchval(command, *args)
                elif fetchrow:
                    result = await connection.fetchrow(command, *args)
                elif execute:
                    result = await connection.execute(command, *args)
            return result

    async def create_table_users(self):
        sql = """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL UNIQUE,
            full_name VARCHAR(255) NOT NULL,
            phone VARCHAR(32) NOT NULL
        );
        """
        await self.execute(sql, execute=True)

    async def create_table_settings(self):
        sql = """
        CREATE TABLE IF NOT EXISTS settings (
            key VARCHAR(64) PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        await self.execute(sql, execute=True)

    async def get_setting(self, key: str):
        sql = "SELECT value FROM settings WHERE key = $1"
        return await self.execute(sql, key, fetchval=True)

    async def set_setting(self, key: str, value: str):
        sql = """
        INSERT INTO settings (key, value, updated_at)
        VALUES ($1, $2, CURRENT_TIMESTAMP)
        ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
        """
        await self.execute(sql, key, value, execute=True)

    @staticmethod
    def format_args(sql, parameters: dict):
        sql += " AND ".join(
            [f"{item} = ${num}" for num, item in enumerate(parameters.keys(), start=1)]
        )
        return sql, tuple(parameters.values())

    async def add_user(self, user_id: int, full_name: str, phone: str):
        sql = (
            "INSERT INTO users (user_id, full_name, phone) "
            "VALUES ($1, $2, $3) RETURNING *"
        )
        return await self.execute(sql, user_id, full_name, phone, fetchrow=True)

    async def select_user(self, **kwargs):
        sql = "SELECT * FROM users WHERE "
        sql, parameters = self.format_args(sql, kwargs)
        return await self.execute(sql, *parameters, fetchrow=True)

    async def select_all_users(self):
        sql = "SELECT * FROM users"
        return await self.execute(sql, fetch=True)

    async def count_users(self):
        sql = "SELECT COUNT(*) FROM users"
        return await self.execute(sql, fetchval=True)

    async def create_table_chats(self):
        sql = """
        CREATE TABLE IF NOT EXISTS chats (
            chat_id BIGINT PRIMARY KEY,
            title VARCHAR(255),
            added_by BIGINT,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            diller_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        await self.execute(sql, execute=True)
        # Migration for pre-existing chats tables (idempotent).
        await self.execute(
            "ALTER TABLE chats ADD COLUMN IF NOT EXISTS diller_id INTEGER",
            execute=True,
        )

    async def upsert_pending_chat(self, chat_id: int, title: str, added_by: int):
        sql = """
        INSERT INTO chats (chat_id, title, added_by, status, updated_at)
        VALUES ($1, $2, $3, 'pending', CURRENT_TIMESTAMP)
        ON CONFLICT (chat_id) DO UPDATE
            SET title = EXCLUDED.title,
                added_by = EXCLUDED.added_by,
                status = 'pending',
                updated_at = CURRENT_TIMESTAMP
        """
        await self.execute(sql, chat_id, title, added_by, execute=True)

    async def set_chat_status(self, chat_id: int, status: str):
        sql = """
        UPDATE chats
        SET status = $2, updated_at = CURRENT_TIMESTAMP
        WHERE chat_id = $1
        RETURNING *
        """
        return await self.execute(sql, chat_id, status, fetchrow=True)

    async def get_chat(self, chat_id: int):
        sql = "SELECT * FROM chats WHERE chat_id = $1"
        return await self.execute(sql, chat_id, fetchrow=True)

    async def set_chat_diller(self, chat_id: int, diller_id: int):
        """Link a chat to a diller and mark it approved in one go."""
        sql = """
        UPDATE chats
        SET diller_id = $2, status = 'approved', updated_at = CURRENT_TIMESTAMP
        WHERE chat_id = $1
        RETURNING *
        """
        return await self.execute(sql, chat_id, diller_id, fetchrow=True)

    async def create_table_dillers(self):
        sql = """
        CREATE TABLE IF NOT EXISTS dillers (
            id INTEGER PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            inn VARCHAR(64),
            phone_number VARCHAR(64),
            address TEXT,
            responsible_person VARCHAR(255),
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        await self.execute(sql, execute=True)

    async def upsert_diller(
        self,
        diller_id: int,
        name: str,
        inn: str = None,
        phone_number: str = None,
        address: str = None,
        responsible_person: str = None,
    ):
        sql = """
        INSERT INTO dillers (
            id, name, inn, phone_number, address, responsible_person, updated_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO UPDATE
            SET name = EXCLUDED.name,
                inn = EXCLUDED.inn,
                phone_number = EXCLUDED.phone_number,
                address = EXCLUDED.address,
                responsible_person = EXCLUDED.responsible_person,
                updated_at = CURRENT_TIMESTAMP
        RETURNING *
        """
        return await self.execute(
            sql,
            diller_id,
            name,
            inn,
            phone_number,
            address,
            responsible_person,
            fetchrow=True,
        )

    async def get_diller(self, diller_id: int):
        sql = "SELECT * FROM dillers WHERE id = $1"
        return await self.execute(sql, diller_id, fetchrow=True)

    async def create_table_diller_chats(self):
        sql = """
        CREATE TABLE IF NOT EXISTS diller_chats (
            diller_id INTEGER NOT NULL,
            diller_name VARCHAR(255) NOT NULL,
            chat_id BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (diller_id, chat_id)
        );
        """
        await self.execute(sql, execute=True)

    async def add_diller_chat(
        self, diller_id: int, diller_name: str, chat_id: int
    ):
        sql = """
        INSERT INTO diller_chats (diller_id, diller_name, chat_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (diller_id, chat_id) DO UPDATE
            SET diller_name = EXCLUDED.diller_name
        RETURNING *
        """
        return await self.execute(
            sql, diller_id, diller_name, chat_id, fetchrow=True
        )

    async def get_diller_chats(self, diller_id: int):
        sql = "SELECT * FROM diller_chats WHERE diller_id = $1 ORDER BY created_at"
        return await self.execute(sql, diller_id, fetch=True)

    async def get_diller_ids_by_chat_id(self, chat_id: int):
        """Return list of diller_id rows linked to a given chat_id (Telegram user)."""
        sql = "SELECT diller_id FROM diller_chats WHERE chat_id = $1"
        rows = await self.execute(sql, chat_id, fetch=True)
        return [r["diller_id"] for r in (rows or [])]
