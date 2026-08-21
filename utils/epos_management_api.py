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
from datetime import date, timedelta
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
    """GET /v1/cashdesks/fiscal/{id} 404 — fiscal ID management'da yo'q."""


class EposMgmtFiscalModuleNotFound(EposMgmtAPIError):
    """PATCH block-date 404 FISCAL_MODULE_NOT_FOUND — cashdesk bor lekin
    fiskal modul o'chirilgan/detach bo'lgan. Retry befoyda."""


def _to_day_first(date_str: str) -> str:
    """`YYYY-MM-DD` → `DD.MM.YYYY`; aks holda qoldiradi."""
    s = (date_str or "").strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-" and s[:4].isdigit():
        y, m, d = s.split("-")
        return f"{d}.{m}.{y}"
    return s


def _parse_date(date_str: str) -> date:
    """`YYYY-MM-DD` yoki `DD.MM.YYYY` → `date` ob'ekti."""
    s = (date_str or "").strip()
    if len(s) == 10:
        if s[4] == "-":
            return date.fromisoformat(s)
        if s[2] == ".":
            d, m, y = s.split(".")
            return date(int(y), int(m), int(d))
    raise ValueError(f"Noto'g'ri sana format: {date_str!r}")


def _fmt_day_first(d: date) -> str:
    return d.strftime("%d.%m.%Y")


class EposManagementAPI:
    def __init__(self):
        self.base_url = config.EPOS_MGMT_API_URL.rstrip("/")
        self.auth_url = f"{self.base_url}/v1/users/authorize"

        self._refresh_lock: Optional[asyncio.Lock] = None
        self._last_refresh_at: float = 0.0
        # Persistent session — DNS/TCP/TLS har safar qayta o'rnatilmaydi.
        # Lazily yaratiladi (event loop import paytida bo'lmasligi mumkin).
        self._session: Optional[aiohttp.ClientSession] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        return self._refresh_lock

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False, limit=10),
            )
        return self._session

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

            session = self._get_session()
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
            session = self._get_session()
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

    async def get_cashdesk_by_fiscal(self, fiscal_id: str) -> dict:
        """GET /v1/cashdesks/fiscal/{fiscalId} — to'g'ridan qidirish.

        Yangi dedicated endpoint (eski ?fiscalId= pagination o'rniga).
        404 → EposMgmtCashdeskNotFound.
        """
        if not fiscal_id:
            raise EposMgmtAPIError("fiscal_id пустой")

        try:
            resp = await self.request("GET", f"/v1/cashdesks/fiscal/{fiscal_id}")
        except EposMgmtAPIError as e:
            if "[404]" in str(e):
                raise EposMgmtCashdeskNotFound(
                    f"cashdesk fiscalID={fiscal_id} management'da topilmadi"
                ) from e
            raise

        doc = (resp or {}).get("data") or resp
        if not isinstance(doc, dict) or not doc:
            raise EposMgmtCashdeskNotFound(
                f"cashdesk fiscalID={fiscal_id}: kutilmagan javob {resp!r}"
            )
        return doc

    async def create_fiscal_module(
        self,
        fiscal_id: str,
        virtual_id: str,
        comment: str = "",
    ) -> dict:
        """POST /v1/fiscalmodule/create — yangi fiskal modulni bazaga qo'shadi.

        fiscal_id  — fiskal raqami (masalan LG420230650187)
        virtual_id — zavod seriya raqami (masalan 312325)
        """
        if not fiscal_id:
            raise EposMgmtAPIError("fiscal_id bo'sh")
        if not virtual_id:
            raise EposMgmtAPIError("virtual_id bo'sh")

        body = {
            "fiscalID": fiscal_id,
            "virtualID": str(virtual_id),
            "comment": comment or "",
        }
        resp = await self.request("POST", "/v1/fiscalmodule/create", json=body)
        return (resp or {}).get("data") or resp or {}

    async def update_cashdesk_block_date(
        self,
        fiscal_id: str,
        block_date: str,
        expired_at: Optional[str] = None,
    ) -> dict:
        """PATCH /v1/cashdesks/{fiscalId}/block-date

        API o'zgardi:
        - Path'da endi MongoDB _id emas, to'g'ridan fiscalId ishlatiladi.
        - expiredAt majburiy: blockDate dan kamida 1 kun keyin bo'lishi shart.
          Agar berilmasa — blockDate + 1 kun avtomatik qo'yiladi.

        Sana formatlar: 'YYYY-MM-DD' yoki 'DD.MM.YYYY' (ikkalasi qabul qilinadi).
        """
        if not fiscal_id:
            raise EposMgmtAPIError("fiscal_id пустой")
        if not block_date:
            raise EposMgmtAPIError("block_date пустой")

        try:
            block_d = _parse_date(block_date)
        except ValueError as e:
            raise EposMgmtAPIError(str(e)) from e

        if expired_at:
            try:
                exp_d = _parse_date(expired_at)
            except ValueError as e:
                raise EposMgmtAPIError(str(e)) from e
            if exp_d <= block_d:
                raise EposMgmtAPIError(
                    f"expiredAt ({expired_at}) blockDate ({block_date}) dan katta bo'lishi shart"
                )
        else:
            exp_d = block_d + timedelta(days=1)

        body = {
            "blockDate": _fmt_day_first(block_d),
            "expiredAt": _fmt_day_first(exp_d),
        }

        try:
            resp = await self.request(
                "PATCH",
                f"/v1/cashdesks/{fiscal_id}/block-date",
                json=body,
            )
        except EposMgmtAPIError as e:
            msg = str(e)
            if "[404]" in msg or "FISCAL_MODULE_NOT" in msg:
                raise EposMgmtFiscalModuleNotFound(
                    f"fiscal {fiscal_id}: fiskal modul management'da yo'q"
                ) from e
            raise
        return (resp or {}).get("data") or {}


epos_mgmt_api = EposManagementAPI()
