import asyncio
import logging
import time
from typing import Any, Optional

import aiohttp
from yarl import URL

from data import config
from loader import db

TOKEN_KEY = "epos_token"

# Cazad бэкенд ограничивает число активных токенов на пользователя (порядка
# нескольких десятков). Если 20+ обработчиков стартуют одновременно с
# просроченным токеном, каждый уходит рефрешить и мы моментально сжигаем
# квоту, получая 403 «Maximum amount of tokens allowed per user exceeded».
# Cooldown + lock: ровно один рефреш в это окно, остальные ждут и берут
# уже обновлённый токен из БД.
_REFRESH_COOLDOWN_SEC = 30


class EposAPIError(Exception):
    pass


class EposAPI:
    def __init__(self):
        self.base_url = config.EPOS_API_URL.rstrip("/")
        self.auth_url = f"{self.base_url}/auth/login/"
        # Session-based auth (для /billing/... эндпоинтов, которые
        # не принимают TokenAuthentication). Cookies хранятся в памяти,
        # инвалидируются при рестарте бота.
        self._cookie_jar: Optional[aiohttp.CookieJar] = None

        # Refresh coalescing — единый lock и timestamp последнего успешного
        # логина. Не даёт 20 параллельным хендлерам сжечь per-user token
        # quota одновременными POST /auth/login/.
        # Lock создаётся лениво на первом вызове refresh_token: на Py3.8
        # asyncio.Lock() при __init__ ловил бы «attached to a different
        # loop», т.к. loop ещё не запущен на этапе import модуля.
        self._refresh_lock: Optional[asyncio.Lock] = None
        self._last_refresh_at: float = 0.0

    def _get_lock(self) -> asyncio.Lock:
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        return self._refresh_lock

    async def refresh_token(self) -> str:
        """
        Авторизоваться по телефону/паролю из .env, получить токен и сохранить
        его в БД (таблица `settings`, ключ `epos_token`).

        Если refresh уже был выполнен в течение последних
        `_REFRESH_COOLDOWN_SEC` секунд (например, другим параллельным
        хендлером в бурсте) — возвращаем текущий токен из БД без
        повторного POST на /auth/login/. Это защищает от сжигания
        per-user token quota на cazad.
        """
        if not config.EPOS_PHONE or not config.EPOS_PASSWORD:
            raise EposAPIError("EPOS_PHONE / EPOS_PASSWORD не заданы в .env")

        async with self._get_lock():
            # Пока мы ждали lock, другой воркер уже мог обновить токен.
            elapsed = time.monotonic() - self._last_refresh_at
            if elapsed < _REFRESH_COOLDOWN_SEC:
                cached = await db.get_setting(TOKEN_KEY)
                if cached:
                    logging.debug(
                        "E-POS refresh_token: skipped (last refresh %.1fs ago)",
                        elapsed,
                    )
                    return cached

            payload = {
                "phone": config.EPOS_PHONE,
                "password": config.EPOS_PASSWORD,
            }
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False)
            ) as session:
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
            self._last_refresh_at = time.monotonic()
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
        Авторизованный запрос. Берёт токен из БД через get_token().

        ВНИМАНИЕ: автообновление на 401 ВРЕМЕННО ОТКЛЮЧЕНО для дебага
        ситуации, когда /billing/... стабильно возвращает 401 и бот
        сжигает per-user token quota на /auth/login/. Сейчас если
        API отвечает 401, мы просто кидаем EposAPIError без попытки
        перевыпустить токен. Refresh случается только если в БД
        вообще нет токена (через get_token()).
        """
        url = (
            path
            if path.startswith("http")
            else f"{self.base_url}/{path.lstrip('/')}"
        )
        token = await self.get_token()

        headers = {"Authorization": f"Token {token}"}
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False)
        ) as session:
            async with session.request(
                method, url, json=json, headers=headers
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise EposAPIError(
                        f"{method} {url} [{resp.status}]: {text}"
                    )
                if not text:
                    return None
                try:
                    return await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    return text

    async def get_business(self, virtual_number: str) -> Any:
        """Fetch business info by virtual_number (zavod number)."""
        return await self.request(
            "GET", f"/v1/all-business/?virtual_number={virtual_number}"
        )

    # ---------------------------------------------------------------
    # Session-based аутентификация для эндпоинтов /billing/api/v3/...
    # которые работают через Django Session + X-CSRFToken, не через
    # DRF TokenAuthentication.
    # ---------------------------------------------------------------

    async def session_login(self) -> aiohttp.CookieJar:
        """POST /auth/login/ как делает Swagger UI: ловим выставленные
        сервером cookies (sessionid + csrftoken) и сохраняем их в памяти.
        Затем эти cookies используются в billing_request()."""
        if not config.EPOS_PHONE or not config.EPOS_PASSWORD:
            raise EposAPIError("EPOS_PHONE / EPOS_PASSWORD не заданы в .env")

        payload = {
            "phone": config.EPOS_PHONE,
            "password": config.EPOS_PASSWORD,
        }
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(
            cookie_jar=jar,
            connector=aiohttp.TCPConnector(ssl=False),
        ) as session:
            async with session.post(self.auth_url, json=payload) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise EposAPIError(
                        f"session login failed [{resp.status}]: {body}"
                    )

        self._cookie_jar = jar
        logging.info("E-POS session login OK")
        return jar

    def _get_csrf(self) -> Optional[str]:
        """Достать csrftoken из текущего cookie_jar (если он есть)."""
        if not self._cookie_jar:
            return None
        try:
            cookies = self._cookie_jar.filter_cookies(URL(self.base_url))
        except Exception:
            return None
        csrf = cookies.get("csrftoken")
        return csrf.value if csrf else None

    async def billing_request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
    ) -> Any:
        """Запрос к /billing/... через session+CSRF, как делает Swagger.
        На 401/403 один раз re-login и повторяем."""
        url = (
            path
            if path.startswith("http")
            else f"{self.base_url}/{path.lstrip('/')}"
        )

        if not self._cookie_jar:
            await self.session_login()

        for attempt in range(2):
            csrf = self._get_csrf()
            headers = {"Referer": self.base_url}
            if csrf:
                headers["X-CSRFToken"] = csrf

            async with aiohttp.ClientSession(
                cookie_jar=self._cookie_jar,
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                async with session.request(
                    method, url, json=json, headers=headers
                ) as resp:
                    if resp.status in (401, 403) and attempt == 0:
                        await self.session_login()
                        continue
                    text = await resp.text()
                    if resp.status >= 400:
                        raise EposAPIError(
                            f"{method} {url} [{resp.status}]: {text}"
                        )
                    if not text:
                        return None
                    try:
                        return await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        return text

        raise EposAPIError(f"{method} {url}: session retry exhausted")


epos_api = EposAPI()


async def authed_http(
    method: str,
    url: str,
    token: str,
    *,
    json: Optional[dict] = None,
) -> Any:
    """
    HTTP-вызов с заголовком `Authorization: Token <token>` и автообновлением
    токена на 401: один раз дергает `epos_api.refresh_token()` и повторяет.

    Используется отдельными standalone-функциями (`get_dillers`,
    `get_business_by_name`, `update_business`, `update_branch`,
    `create_branch` и т.п.), которым нужен явно передаваемый токен, но
    при этом обновление на 401 тоже нужно.

    Возвращает распарсенный JSON, либо строку (если ответ не JSON), либо
    None (пустой ответ). На любой статус >= 400 (кроме первого 401) бросает
    EposAPIError.
    """
    for attempt in range(2):
        headers = {"Authorization": f"Token {token}"}
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False)
        ) as session:
            async with session.request(
                method, url, headers=headers, json=json
            ) as resp:
                text = await resp.text()
                if resp.status == 401 and attempt == 0:
                    token = await epos_api.refresh_token()
                    continue
                if resp.status >= 400:
                    raise EposAPIError(
                        f"{method} {url} [{resp.status}]: {text}"
                    )
                if not text:
                    return None
                try:
                    return await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    return text

    raise EposAPIError(f"{method} {url}: auth retry exhausted")
