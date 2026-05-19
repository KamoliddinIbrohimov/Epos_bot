import logging
from typing import Any, Optional

import aiohttp

from data import config
from loader import db

TOKEN_KEY = "epos_token"


class EposAPIError(Exception):
    pass


class EposAPI:
    def __init__(self):
        self.base_url = config.EPOS_API_URL.rstrip("/")
        self.auth_url = f"{self.base_url}/auth/login/"

    async def refresh_token(self) -> str:
        """
        Авторизоваться по телефону/паролю из .env, получить токен и сохранить
        его в БД (таблица `settings`, ключ `epos_token`).

        Вызывается один раз — при первом обращении к API, либо повторно,
        если текущий токен устарел (HTTP 401 на любом запросе).
        Это единственное место, где выполняется POST на /auth/login/.
        """
        if not config.EPOS_PHONE or not config.EPOS_PASSWORD:
            raise EposAPIError("EPOS_PHONE / EPOS_PASSWORD не заданы в .env")

        payload = {
            "phone": config.EPOS_PHONE,
            "password": config.EPOS_PASSWORD,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(self.auth_url, json=payload) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise EposAPIError(f"auth failed [{resp.status}]: {body}")
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    raise EposAPIError(f"auth response is not JSON: {body}")

        token = self._extract_token(data)
        if not token:
            raise EposAPIError(f"токен не найден в ответе: {data}")

        await db.set_setting(TOKEN_KEY, token)
        logging.info("E-POS токен сохранён в БД")
        return token

    @staticmethod
    def _extract_token(data: Any) -> Optional[str]:
        if not isinstance(data, dict):
            return None
        for key in ("token", "access", "access_token", "key"):
            if data.get(key):
                return str(data[key])
        nested = data.get("data") if isinstance(data.get("data"), dict) else None
        if nested:
            for key in ("token", "access", "access_token"):
                if nested.get(key):
                    return str(nested[key])
        return None

    async def get_token(self) -> str:
        """
        Прочитать токен из БД. Если в БД ничего нет — один раз вызвать
        refresh_token(), сохранить и вернуть. На последующих обращениях
        просто читает из БД, без сетевых запросов.
        """
        token = await db.get_setting(TOKEN_KEY)
        if not token:
            token = await self.refresh_token()
        return token

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
    ) -> Any:
        """
        Любой авторизованный запрос. Берёт токен из БД через get_token().
        Если API ответил 401 (токен устарел) — вызывает refresh_token(),
        обновляет токен в БД и повторяет запрос один раз.
        """
        url = (
            path
            if path.startswith("http")
            else f"{self.base_url}/{path.lstrip('/')}"
        )
        token = await self.get_token()

        for attempt in range(2):
            headers = {"authorization": f"token {token}"}
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method, url, json=json, headers=headers
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        token = await self.refresh_token()
                        continue
                    text = await resp.text()
                    if resp.status >= 400:
                        raise EposAPIError(f"{method} {url} [{resp.status}]: {text}")
                    if not text:
                        return None
                    try:
                        return await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        return text

        raise EposAPIError("Authentication retry exhausted")

    async def get_business(self, virtual_number: str) -> Any:
        """Fetch business info by virtual_number (zavod number)."""
        return await self.request(
            "GET", f"/v1/all-business/?virtual_number={virtual_number}"
        )


epos_api = EposAPI()
