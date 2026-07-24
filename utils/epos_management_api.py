"""Client for the second E-POS backend: api.management.epos.uz.

Auth model:
  * Endpoint POST /v1/users/authorize принимает
    { phone, password, otp, verificationToken } и возвращает JSON,
    из которого мы достаём `accessToken` (варианты имён покрыты
    в `_extract_token`).
  * Токен сохраняется в таблице `settings` под ключом
    `epos_management_token`, чтобы переживать рестарт бота.
  * На 401 при защищённом запросе один раз дёргаем refresh_token()
    и повторяем; больше не пытаемся — иначе можно сжечь rate-limit.

OTP / verificationToken не требуются для аккаунтов без 2FA — оставь
соответствующие переменные .env пустыми, и они не пойдут в тело
запроса.
"""

import asyncio
import logging
import time
from typing import Any, Optional

import aiohttp

from data import config
from loader import db


TOKEN_KEY = "epos_management_token"

# Как и cazad, management-бэкенд может иметь ограничение на частоту login'ов.
# Coalescing одновременных refresh'ей чтобы не сжигать сессии.
_MGMT_REFRESH_COOLDOWN_SEC = 30


class EposMgmtAPIError(Exception):
    pass


class EposMgmtCashdeskNotFound(EposMgmtAPIError):
    """Пейджинг закончился, а cashdesk с нужным fiscalID так и не встретился."""


def _to_day_first(date_str: str) -> str:
    """`YYYY-MM-DD` -> `DD.MM.YYYY`; иначе возвращает как есть.

    Управленческий бэкенд `getParsedDate` некорректно разворачивает
    ISO-строки (случайно даёт правильный результат — но полагаться нельзя),
    поэтому нормализуем к day-first формату сами."""
    s = (date_str or "").strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-" and s[:4].isdigit():
        y, m, d = s.split("-")
        return f"{d}.{m}.{y}"
    return s


class EposManagementAPI:
    def __init__(self):
        self.base_url = config.EPOS_MGMT_API_URL.rstrip("/")
        self.auth_url = f"{self.base_url}/v1/users/authorize"

        # Refresh coalescing: единый lock и timestamp последнего логина.
        # Защищает от одновременных POST /authorize в бурсте параллельных
        # хендлеров. Lock создаётся лениво (Py3.8 привязывает asyncio.Lock
        # к текущему loop; при импорте модуля loop ещё нет).
        self._refresh_lock: Optional[asyncio.Lock] = None
        self._last_refresh_at: float = 0.0

    def _get_lock(self) -> asyncio.Lock:
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        return self._refresh_lock

    async def refresh_token(self) -> str:
        """Логин по .env, извлекаем accessToken, сохраняем в БД.

        Если refresh уже был выполнен в течение последних
        `_MGMT_REFRESH_COOLDOWN_SEC` секунд другим воркером — возвращаем
        текущий токен из БД, не дёргая /authorize повторно.
        """
        if not config.EPOS_MGMT_PHONE or not config.EPOS_MGMT_PASSWORD:
            raise EposMgmtAPIError(
                "EPOS_MGMT_PHONE / EPOS_MGMT_PASSWORD не заданы в .env"
            )

        async with self._get_lock():
            elapsed = time.monotonic() - self._last_refresh_at
            if elapsed < _MGMT_REFRESH_COOLDOWN_SEC:
                cached = await db.get_setting(TOKEN_KEY)
                if cached:
                    logging.debug(
                        "mgmt refresh_token: skipped (last refresh %.1fs ago)",
                        elapsed,
                    )
                    return cached

            payload = {
                "phone": config.EPOS_MGMT_PHONE,
                "password": config.EPOS_MGMT_PASSWORD,
            }
            # OTP и verificationToken отправляем, только если реально заданы.
            # Пустые строки почти наверняка спровоцируют 400 у бэкенда,
            # требующего 2FA — но если 2FA не включён, поля просто не нужны.
            if config.EPOS_MGMT_OTP:
                payload["otp"] = config.EPOS_MGMT_OTP
            if config.EPOS_MGMT_VERIFICATION_TOKEN:
                payload["verificationToken"] = config.EPOS_MGMT_VERIFICATION_TOKEN

            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False)
            ) as session:
                async with session.post(self.auth_url, json=payload) as resp:
                    body = await resp.text()
                    if resp.status >= 400:
                        raise EposMgmtAPIError(
                            f"mgmt auth failed [{resp.status}]: {body}"
                        )
                    try:
                        data = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        raise EposMgmtAPIError(
                            f"mgmt auth response is not JSON: {body}"
                        )

            token = self._extract_token(data)
            if not token:
                raise EposMgmtAPIError(f"accessToken не найден в ответе: {data}")

            await db.set_setting(TOKEN_KEY, token)
            self._last_refresh_at = time.monotonic()
            logging.info("E-POS Management accessToken сохранён в БД")
            return token

    @staticmethod
    def _extract_token(data: Any) -> Optional[str]:
        if not isinstance(data, dict):
            return None
        for key in ("accessToken", "access_token", "token", "access"):
            if data.get(key):
                return str(data[key])
        # Некоторые обёртки кладут результат под .data / .result.
        for wrap_key in ("data", "result"):
            nested = data.get(wrap_key)
            if isinstance(nested, dict):
                for key in ("accessToken", "access_token", "token", "access"):
                    if nested.get(key):
                        return str(nested[key])
        return None

    async def get_token(self) -> str:
        """Читаем токен из БД; если пусто — логинимся один раз."""
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
        """Auth'd HTTP через `Authorization: Bearer <accessToken>`.

        На 401 **или 403** один раз дёргаем refresh_token() и повторяем:
          - 401 — токен истёк / не валиден;
          - 403 — обычно 'ролью не разрешено', но иногда бэкенд отдаёт
                  403 вместо 401 когда токен просрочен, либо когда роль
                  расширили, а старый токен ещё несёт устаревший набор
                  прав (заведён до апдейта). Повторный login освежает
                  claims — если 403 после этого сохраняется, значит это
                  настоящий permission-deny.
        """
        url = (
            path
            if path.startswith("http")
            else f"{self.base_url}/{path.lstrip('/')}"
        )
        token = await self.get_token()

        for attempt in range(2):
            headers = {"Authorization": f"Bearer {token}"}
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False)
            ) as session:
                async with session.request(
                    method, url, json=json, headers=headers
                ) as resp:
                    text = await resp.text()
                    if resp.status in (401, 403) and attempt == 0:
                        logging.warning(
                            "mgmt %s -> [%s], refreshing token and retrying",
                            url, resp.status,
                        )
                        token = await self.refresh_token()
                        continue
                    if resp.status >= 400:
                        raise EposMgmtAPIError(
                            f"{method} {url} [{resp.status}]: {text}"
                        )
                    if not text:
                        return None
                    try:
                        return await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        return text

        raise EposMgmtAPIError(f"{method} {url}: auth retry exhausted")

    # ---------------------------------------------------------------
    # High-level endpoints
    # ---------------------------------------------------------------

    async def get_cashdesk_by_fiscal(
        self,
        fiscal_id: str,
        *,
        page_limit: int = 50,
        max_pages: int = 60,
    ) -> dict:
        """Найти cashdesk по fiscalID и вернуть его doc целиком.

        Сервер игнорирует ?fiscalId= (кладёт его в query, но фильтрацию
        не делает — возвращает первую страницу всей коллекции ~2850 доков),
        поэтому листаем страницы и ищем совпадение по
        `doc.fiscalModule.fiscalID` на клиенте.

        Не нашли до конца пагинации (либо до `max_pages`) → EposMgmtCashdeskNotFound.
        Пустой fiscal_id → EposMgmtAPIError.
        """
        if not fiscal_id:
            raise EposMgmtAPIError("fiscal_id пустой")

        page = 1
        scanned = 0
        while page <= max_pages:
            resp = await self.request(
                "GET",
                f"/v1/cashdesks?fiscalId={fiscal_id}"
                f"&page={page}&limit={page_limit}",
            )
            data = (resp or {}).get("data") or {}
            docs = data.get("docs") or []
            scanned += len(docs)
            for doc in docs:
                fm = doc.get("fiscalModule") or {}
                if fm.get("fiscalID") == fiscal_id:
                    return doc
            if not data.get("hasNextPage"):
                raise EposMgmtCashdeskNotFound(
                    f"cashdesk с fiscalID={fiscal_id} не найден "
                    f"(просмотрено {scanned} записей)"
                )
            page += 1

        raise EposMgmtCashdeskNotFound(
            f"cashdesk с fiscalID={fiscal_id} не найден "
            f"(достигнут лимит max_pages={max_pages}, просмотрено {scanned})"
        )

    async def update_cashdesk_block_date(
        self,
        cashdesk_id: str,
        block_date: str,
        expired_at: Optional[str] = None,
    ) -> dict:
        """PATCH /v1/cashdesks/{_id}/block-date — обновляет только
        licence.blockDate (и опционально licence.expiresAt).

        Path требует MongoDB _id (24 hex, из cashdesk['_id']), не fiscalID
        — иначе 400 CastError. Даты принимаются как 'DD.MM.YYYY' или
        'YYYY-MM-DD' (второе автоматически конвертится в day-first).

        Возвращает полный обновлённый doc из data.
        """
        if not cashdesk_id:
            raise EposMgmtAPIError("cashdesk_id пустой")
        if not block_date:
            raise EposMgmtAPIError("block_date пустой")

        body = {"blockDate": _to_day_first(block_date)}
        if expired_at:
            # ⚠️ В request'е ключ 'expiredAt' (past tense), а в response
            # придёт как 'expiresAt' — так задумано серверной командой.
            body["expiredAt"] = _to_day_first(expired_at)

        resp = await self.request(
            "PATCH",
            f"/v1/cashdesks/{cashdesk_id}/block-date",
            json=body,
        )
        return (resp or {}).get("data") or {}


epos_mgmt_api = EposManagementAPI()
