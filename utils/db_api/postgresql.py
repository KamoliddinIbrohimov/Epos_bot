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
        # Принудительно открываем реальную коннекцию, чтобы любая проблема
        # с авторизацией / сетью падала ровно тут, а не отложенно на первом
        # пользовательском запросе. Это даёт on_startup в app.py шанс
        # поймать InvalidPasswordError и напечатать понятный лог.
        async with self.pool.acquire() as conn:
            await conn.execute("SELECT 1")

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
            group_type VARCHAR(16),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        await self.execute(sql, execute=True)
        # Migrations for pre-existing chats tables (idempotent).
        await self.execute(
            "ALTER TABLE chats ADD COLUMN IF NOT EXISTS diller_id INTEGER",
            execute=True,
        )
        await self.execute(
            "ALTER TABLE chats ADD COLUMN IF NOT EXISTS group_type VARCHAR(16)",
            execute=True,
        )
        await self.execute(
            "ALTER TABLE chats ADD COLUMN IF NOT EXISTS "
            "prodleniya_enabled BOOLEAN NOT NULL DEFAULT FALSE",
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
        """Link a chat to a diller. Group still needs group_type before it's
        fully approved — status remains 'pending' until set_chat_group_type."""
        sql = """
        UPDATE chats
        SET diller_id = $2, updated_at = CURRENT_TIMESTAMP
        WHERE chat_id = $1
        RETURNING *
        """
        return await self.execute(sql, chat_id, diller_id, fetchrow=True)

    async def set_chat_group_type(self, chat_id: int, group_type: str):
        """Set group_type ('registration' | 'log') and finalize approval."""
        sql = """
        UPDATE chats
        SET group_type = $2,
            status = 'approved',
            updated_at = CURRENT_TIMESTAMP
        WHERE chat_id = $1
        RETURNING *
        """
        return await self.execute(sql, chat_id, group_type, fetchrow=True)

    async def set_chat_prodleniya(self, chat_id: int, enabled: bool):
        """Toggle prodleniya (renewal via plain-text messages) for a chat.
        Only meaningful for group_type='registration' rows — other types
        ignore the flag."""
        sql = """
        UPDATE chats
        SET prodleniya_enabled = $2,
            updated_at = CURRENT_TIMESTAMP
        WHERE chat_id = $1
        RETURNING *
        """
        return await self.execute(sql, chat_id, enabled, fetchrow=True)

    async def create_table_prodleniya_pending(self):
        """Очередь prodleniya-запросов к management API, которые упали
        транзиентно (5xx/timeout). Воркер раз в N минут пытается их
        повторно применить."""
        sql = """
        CREATE TABLE IF NOT EXISTS prodleniya_pending (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            message_id BIGINT,
            user_id BIGINT,
            diller_id INTEGER,
            fiscal_id TEXT NOT NULL,
            target_iso DATE NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INT NOT NULL DEFAULT 0,
            last_error TEXT,
            next_try_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        await self.execute(sql, execute=True)
        await self.execute(
            "CREATE INDEX IF NOT EXISTS prodleniya_pending_ready_idx "
            "ON prodleniya_pending (status, next_try_at)",
            execute=True,
        )

    async def enqueue_prodleniya(
        self,
        *,
        chat_id: int,
        message_id: int,
        user_id: int,
        diller_id: int,
        fiscal_id: str,
        target_iso: str,
        error: str,
    ):
        sql = """
        INSERT INTO prodleniya_pending
            (chat_id, message_id, user_id, diller_id, fiscal_id,
             target_iso, last_error)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """
        return await self.execute(
            sql, chat_id, message_id, user_id, diller_id,
            fiscal_id, target_iso, error[:1000],
            fetchval=True,
        )

    async def list_pending_prodleniya(self, limit: int = 50):
        sql = """
        SELECT * FROM prodleniya_pending
        WHERE status = 'pending' AND next_try_at <= CURRENT_TIMESTAMP
        ORDER BY next_try_at
        LIMIT $1
        """
        rows = await self.execute(sql, limit, fetch=True)
        return rows or []

    async def mark_prodleniya_done(self, id_: int):
        sql = """
        UPDATE prodleniya_pending
        SET status = 'done', updated_at = CURRENT_TIMESTAMP
        WHERE id = $1
        """
        await self.execute(sql, id_, execute=True)

    async def mark_prodleniya_giveup(self, id_: int, error: str):
        sql = """
        UPDATE prodleniya_pending
        SET status = 'giveup', last_error = $2, updated_at = CURRENT_TIMESTAMP
        WHERE id = $1
        """
        await self.execute(sql, id_, error[:1000], execute=True)

    async def reschedule_prodleniya(
        self, id_: int, next_try_at, error: str
    ):
        sql = """
        UPDATE prodleniya_pending
        SET attempts = attempts + 1,
            last_error = $2,
            next_try_at = $3,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = $1
        """
        await self.execute(sql, id_, error[:1000], next_try_at, execute=True)

    async def list_registration_chats(self):
        """Return approved registration chats with diller info attached.
        Used by admin settings screen."""
        sql = """
        SELECT c.chat_id, c.title, c.diller_id, c.prodleniya_enabled,
               d.name AS diller_name
        FROM chats c
        LEFT JOIN dillers d ON d.id = c.diller_id
        WHERE c.status = 'approved' AND c.group_type = 'registration'
        ORDER BY c.updated_at DESC
        """
        rows = await self.execute(sql, fetch=True)
        return rows or []

    async def get_log_chats_for_diller(self, diller_id: int):
        """Return chat_id list of 'log' groups linked to the given diller."""
        sql = """
        SELECT chat_id FROM chats
        WHERE diller_id = $1
          AND group_type = 'log'
          AND status = 'approved'
        """
        rows = await self.execute(sql, diller_id, fetch=True)
        return [r["chat_id"] for r in (rows or [])]

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
