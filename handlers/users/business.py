import html
import logging
import os
import tempfile
from datetime import date

from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.callback_data import CallbackData

from data import config
from loader import bot, db, dp
from utils.diller import get_user_diller_name
from utils.epos_api import EposAPIError, authed_http, epos_api
from utils.notify_groups import notify_log_groups
from utils.parse_pdf import PdfParseError, format_analysis, parse_business_pdf
from utils.state_control import prompt_continue_or_exit, save_prompt

CAPTION_LIMIT = 1024

UPDATABLE_FIELDS = (
    "name",
    "owner",
    "business_type",
    "diller",
    "auto_update",
    "auth_key",
    "virtual_number",
    "TIN",
    "version_info",
    "status",
    "price",
    "pinfl_tin",
    "blocked_date",
    "reason",
)

BRANCH_UPDATABLE_FIELDS = (
    "name",
    "address",
    "contact_person",
    "contact_phone",
    "business",
    "city",
)


class FiscalModule(StatesGroup):
    waiting_for_factory_id = State()
    confirming = State()


class NewClientClaim(StatesGroup):
    waiting_for_auth_key = State()
    confirming = State()


fiscal_cb = CallbackData("fisk", "action")
new_client_cb = CallbackData("newc", "action")


def fiscal_confirm_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(
            "✅ Подтвердить", callback_data=fiscal_cb.new(action="confirm")
        ),
        InlineKeyboardButton(
            "🔄 Отправить заново", callback_data=fiscal_cb.new(action="resend")
        ),
    )
    return kb


def new_client_confirm_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(
            "✅ Подтвердить", callback_data=new_client_cb.new(action="confirm")
        ),
        InlineKeyboardButton(
            "🔄 Отправить заново", callback_data=new_client_cb.new(action="resend")
        ),
    )
    return kb


async def get_business(virtual_number, token):
    """GET /v1/all-business/?virtual_number=... — auto-retry on 401."""
    url = (
        f"{config.EPOS_API_URL.rstrip('/')}"
        f"/v1/all-business/?virtual_number={virtual_number}"
    )
    return await authed_http("GET", url, token)


async def update_business(business_id, token, **fields):
    """PUT /v1/businesses/{business_id}/ — auto-retry on 401."""
    url = f"{config.EPOS_API_URL.rstrip('/')}/v1/businesses/{business_id}/"
    return await authed_http("PUT", url, token, json=fields)


async def update_branch(branch_id, token, **fields):
    """PUT /v1/branches/{branch_id}/ — auto-retry on 401."""
    url = f"{config.EPOS_API_URL.rstrip('/')}/v1/branches/{branch_id}/"
    return await authed_http("PUT", url, token, json=fields)


async def create_branch(token, **fields):
    """POST /v1/branches/ — auto-retry on 401."""
    url = f"{config.EPOS_API_URL.rstrip('/')}/v1/branches/"
    return await authed_http("POST", url, token, json=fields)


def _pick_business(payload) -> dict:
    if isinstance(payload, list):
        return payload[0] if payload else {}
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list) and results:
            return results[0]
        return payload
    return {}


def _flatten_fk(value):
    if isinstance(value, dict):
        return value.get("id") or value.get("pk")
    return value


def _calc_blocked_date(today: date = None) -> str:
    """
    < 20-го числа       -> 1-е число следующего месяца
    20-го числа и позже -> 1-е число месяца после следующего
    """
    today = today or date.today()
    months_ahead = 1 if today.day < 20 else 2
    month = today.month + months_ahead
    year = today.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    return date(year, month, 1).isoformat()


@dp.message_handler(
    lambda m: bool(
        m.document
        and m.document.file_name
        and m.document.file_name.lower().endswith(".pdf")
    ),
    chat_type=types.ChatType.PRIVATE,
    content_types=types.ContentType.DOCUMENT,
)
async def handle_business_pdf(message: types.Message, state: FSMContext):
    doc = message.document

    if not await db.select_user(user_id=message.from_user.id):
        await message.answer("Сначала пройдите регистрацию через /start.")
        return

    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        await doc.download(destination_file=tmp_path)
        try:
            parsed = parse_business_pdf(tmp_path)
        except PdfParseError as e:
            await message.answer(f"Не удалось разобрать PDF: {e}")
            return
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    zavod = parsed.get("zavod")
    holati = parsed.get("holati")
    fiscal_modules = parsed.get("fiskal_modules") or []

    # === Шаг 1: проверка дилера (DB-only, ДО любого API-вызова) ===
    diller_ids = await db.get_diller_ids_by_chat_id(message.from_user.id)
    if not diller_ids:
        await message.answer("⛔ Эта функция вам недоступна.")
        return

    # === Шаг 2: токен ===
    try:
        token = await epos_api.get_token()
    except EposAPIError as e:
        logging.exception("get_token failed")
        await message.answer(f"⚠️ get_token: {html.escape(str(e))}")
        return

    text = format_analysis(parsed)

    # === Шаг 3: "Новый клиент" — ищем по zavod (virtual_number) ===
    if holati == "Новый клиент":
        if not zavod or zavod == "—":
            await message.answer("<i>virtual_number в PDF не найден.</i>")
            return
        try:
            business_data = await get_business(zavod, token)
        except EposAPIError as e:
            logging.exception("get_business failed")
            await message.answer(f"⚠️ get_business: {html.escape(str(e))}")
            return

        # Если business уже зарегистрирован и принадлежит этому дилеру —
        # синкаем branch.name + branch.address до того, как уйдём в
        # _maybe_start_new_client_flow.
        business_for_sync = _pick_business(business_data) if business_data else {}
        sync_business_id = (
            business_for_sync.get("id") or business_for_sync.get("business_id")
        )
        business_diller_id = _flatten_fk(business_for_sync.get("diller"))
        logging.info(
            "new-client sync gate: business_id=%s diller_ids=%s "
            "business_diller_id=%s match=%s parsed_org=%r parsed_addr=%r",
            sync_business_id, diller_ids, business_diller_id,
            (business_diller_id in diller_ids) if diller_ids else False,
            parsed.get("organization"), parsed.get("address"),
        )
        if sync_business_id and business_diller_id in diller_ids:
            await _sync_branch_data(
                message,
                business_for_sync,
                sync_business_id,
                parsed.get("organization"),
                parsed.get("address"),
                token,
                business_diller_id,
            )

        await _maybe_start_new_client_flow(
            message, state, parsed, business_data, zavod, doc.file_id, text
        )
        return

    # === Шаг 4: Фискальный/Адрес — ищем по фискальному номеру ===
    business = {}
    business_id = None
    lookup_chain: tuple = ()

    if holati == "Фискальный модуль изменён":
        if len(fiscal_modules) < 2:
            await message.answer(
                "⚠️ Недостаточно данных в PDF для смены фискального модуля."
            )
            return
        lookup_chain = (fiscal_modules[-2], fiscal_modules[-1])
    elif holati == "Адрес изменён":
        if not fiscal_modules:
            await message.answer("⚠️ Фискальный номер не найден в PDF.")
            return
        lookup_chain = (fiscal_modules[-1],)
    else:
        return  # неизвестный статус — игнорим

    from handlers.users.find_business import get_business_by_name
    for fn in lookup_chain:
        if not fn:
            continue
        try:
            response = await get_business_by_name(fn, token)
        except EposAPIError:
            logging.exception("get_business_by_name(%r) failed", fn)
            response = None
        picked = _pick_business(response) if response is not None else {}
        picked_id = picked.get("id") or picked.get("business_id")
        if picked_id:
            business = picked
            business_id = picked_id
            break

    if not business_id:
        await message.answer(
            "⚠️ Бизнес не найден в Cazad ни по одному из фискальных номеров: "
            + ", ".join(
                f"<code>{html.escape(str(fn))}</code>"
                for fn in lookup_chain if fn
            )
        )
        return

    # Ownership check — diller_ids уже получены на шаге 1.
    business_diller_id = _flatten_fk(business.get("diller"))
    if business_diller_id not in diller_ids:
        await message.answer("⛔ Этот клиент вам не принадлежит.")
        return

    await _send_pdf_to_user_and_group(
        message.chat.id,
        message.from_user,
        doc.file_id,
        text,
        diller_id=business_diller_id,
    )

    # Side-check: branch.name и branch.address должны совпадать с
    # organization и address из PDF. Если что-то расходится — синкаем
    # и шлём уведомления.
    await _sync_branch_data(
        message,
        business,
        business_id,
        parsed.get("organization"),
        parsed.get("address"),
        token,
        business_diller_id,
    )

    if holati == "Фискальный модуль изменён":
        await _start_fiscal_change_flow(
            message, state, parsed, business, business_id
        )
    elif holati == "Адрес изменён":
        await _start_address_change_flow(message, parsed, business, business_id)


async def _sync_branch_data(
    message: types.Message,
    business: dict,
    business_id,
    parsed_organization,
    parsed_address,
    token: str,
    diller_id,
) -> None:
    """Логика для уже зарегистрированного клиента:
      * Если у бизнеса вообще нет филиала — создаём новый через create_branch
        с данными из PDF и привязываем к business_id.
      * Если филиал есть, но branch.name или branch.address не совпадает с
        PDF — обновляем расходящиеся поля.
      * Если филиал есть и всё совпадает — тихо выходим.

    На любое реальное действие (создание / обновление) шлём:
      - уведомление пользователю в private chat;
      - уведомление в лог-группы (per-diller + центральный PDF_GROUP_CHAT_ID).
    """
    new_name = str(parsed_organization or "").strip()
    new_addr = str(parsed_address or "").strip()
    name_candidate = new_name and new_name != "—"
    addr_candidate = new_addr and new_addr != "—"

    branches = business.get("branches") or []
    target = next(
        (b for b in branches if isinstance(b, dict) and b.get("id")), None
    )

    # === Ветка 1: филиала нет вообще — создаём. ===
    if not target:
        if not name_candidate and not addr_candidate:
            # Из PDF нечего записать — некорректно создавать «пустой» branch.
            logging.info(
                "sync_branch_data: no target branch and no data in PDF to create one"
            )
            return

        logging.info(
            "sync_branch_data: no branch exists, creating new "
            "with name=%r address=%r",
            new_name or None, new_addr or None,
        )
        try:
            await create_branch(
                token,
                name=new_name or None,
                address=new_addr or None,
                contact_person="User",
                contact_phone="+998",
                business=business_id,
                city=None,
            )
        except EposAPIError as e:
            logging.exception("sync branch data: create_branch failed")
            await message.answer(
                f"⚠️ Не удалось создать филиал: {html.escape(str(e))}"
            )
            return

        change_lines = []
        if name_candidate:
            change_lines.append(
                f"<b>Имя фирмы:</b> <code>{html.escape(new_name)}</code>"
            )
        if addr_candidate:
            change_lines.append(
                f"<b>Адрес:</b> <code>{html.escape(new_addr)}</code>"
            )
        body = "\n".join(change_lines)

        await message.answer(f"✅ Филиал создан и привязан к клиенту.\n{body}")

        user = message.from_user
        diller_name = await get_user_diller_name(user.id) or "—"
        summary = (
            f"🆕 <b>Филиал создан</b>\n"
            f"<b>Diller:</b> {html.escape(str(diller_name))}\n"
            f'От: <a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a> '
            f"(id: <code>{user.id}</code>)\n\n"
            f"{body}"
        )
        try:
            await notify_log_groups(diller_id, summary)
        except Exception as exc:
            logging.exception(
                f"sync branch data: notify_log_groups (create) failed: {exc}"
            )
        return

    # === Ветка 2: филиал есть — сравниваем и при необходимости обновляем. ===
    api_branch_name = (target.get("name") or "").strip()
    api_branch_addr = (target.get("address") or "").strip()

    name_diff = name_candidate and api_branch_name != new_name
    addr_diff = addr_candidate and api_branch_addr.lower() != new_addr.lower()

    logging.info(
        "sync_branch_data: branch_id=%s api_name=%r new_name=%r name_diff=%s "
        "api_addr=%r new_addr=%r addr_diff=%s",
        target.get("id"),
        api_branch_name, new_name, name_diff,
        api_branch_addr, new_addr, addr_diff,
    )

    if not name_diff and not addr_diff:
        return

    branch_id = target["id"]
    payload = {
        key: _flatten_fk(target.get(key))
        for key in BRANCH_UPDATABLE_FIELDS
        if key in target
    }
    if name_diff:
        payload["name"] = new_name
    if addr_diff:
        payload["address"] = new_addr
    payload["business"] = business_id

    try:
        await update_branch(branch_id, token, **payload)
    except EposAPIError as e:
        logging.exception("sync branch data failed")
        await message.answer(
            f"⚠️ Не удалось обновить филиал: {html.escape(str(e))}"
        )
        return

    change_lines = []
    if name_diff:
        change_lines.append(
            f"<b>Имя фирмы:</b> <code>{html.escape(api_branch_name or '—')}</code> "
            f"→ <code>{html.escape(new_name)}</code>"
        )
    if addr_diff:
        change_lines.append(
            f"<b>Адрес:</b> <code>{html.escape(api_branch_addr or '—')}</code> "
            f"→ <code>{html.escape(new_addr)}</code>"
        )
    body = "\n".join(change_lines)

    await message.answer(f"✅ Данные филиала обновлены.\n{body}")

    user = message.from_user
    diller_name = await get_user_diller_name(user.id) or "—"
    summary = (
        f"🏷 <b>Данные филиала обновлены</b>\n"
        f"<b>Diller:</b> {html.escape(str(diller_name))}\n"
        f'От: <a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a> '
        f"(id: <code>{user.id}</code>)\n\n"
        f"{body}"
    )
    try:
        await notify_log_groups(diller_id, summary)
    except Exception as exc:
        logging.exception(f"sync branch data notify_log_groups failed: {exc}")


async def _send_pdf_to_user_and_group(
    chat_id: int,
    user,
    doc_file_id: str,
    text: str,
    diller_name: str = None,
    diller_id: int = None,
) -> None:
    """Send the PDF (by file_id) with analysis text to the originating chat,
    and ALSO forward it to:
      - every 'log' group bound to `diller_id`, and
      - the global PDF_GROUP_CHAT_ID (if configured),
    with diller name + sender info prepended in the caption.
    De-duplicates: each chat receives at most one copy.
    """
    await _send_doc_with_text(chat_id, doc_file_id, text)

    if diller_name is None:
        diller_name = await get_user_diller_name(user.id) or "—"

    name = html.escape(user.full_name)
    group_caption = (
        f"<b>Diller:</b> {html.escape(str(diller_name))}\n"
        f'От: <a href="tg://user?id={user.id}">{name}</a> '
        f"(id: <code>{user.id}</code>)\n\n{text}"
    )

    recipients = []
    seen = set()
    if diller_id is not None:
        for cid in await db.get_log_chats_for_diller(int(diller_id)):
            if cid not in seen:
                seen.add(cid)
                recipients.append(cid)
    central = config.PDF_GROUP_CHAT_ID
    if central and central not in seen:
        recipients.append(central)

    for log_chat_id in recipients:
        try:
            await _send_doc_with_text(log_chat_id, doc_file_id, group_caption)
        except Exception as e:
            logging.exception(
                f"failed to send PDF to log chat {log_chat_id}: {e}"
            )


async def _maybe_start_new_client_flow(
    message: types.Message,
    state: FSMContext,
    parsed: dict,
    business_data,
    virtual_number,
    doc_file_id: str,
    analysis_text: str,
) -> None:
    """Если business в Cazad есть, но dealer=null — запрашиваем auth_key
    у дилера, который прислал PDF, и привязываем клиента к нему."""
    diller_ids = await db.get_diller_ids_by_chat_id(message.from_user.id)
    if not diller_ids:
        await message.answer("⛔ Эта функция вам недоступна.")
        return

    business = _pick_business(business_data) if business_data is not None else {}
    business_id = business.get("id") or business.get("business_id")
    if not business_id:
        await message.answer(
            f"⚠️ Бизнес с virtual_number=<code>{html.escape(str(virtual_number))}</code> "
            f"не найден в базе Cazad. Регистрация невозможна."
        )
        return

    if business.get("auth_key"):
        await message.answer("ℹ️ Этот клиент уже зарегистрирован.")
        return

    user_diller_id = diller_ids[0]
    diller_row = await db.get_diller(user_diller_id)
    diller_name = (
        diller_row["name"] if diller_row and diller_row.get("name")
        else f"id={user_diller_id}"
    )

    fiscal_modules = parsed.get("fiskal_modules") or []
    new_fiscal = fiscal_modules[-1] if fiscal_modules else None
    organization = parsed.get("organization") or "—"
    activity_type = parsed.get("activity_type") or "—"

    await state.update_data(
        nc_business_id=business_id,
        nc_business=business,
        nc_virtual_number=virtual_number,
        nc_organization=organization,
        nc_address=parsed.get("address"),
        nc_stir=parsed.get("stir"),
        nc_business_type=parsed.get("business_type"),
        nc_activity_type=activity_type,
        nc_new_fiscal=new_fiscal,
        nc_user_diller_id=user_diller_id,
        nc_diller_name=diller_name,
        nc_doc_file_id=doc_file_id,
        nc_analysis_text=analysis_text,
    )
    await NewClientClaim.waiting_for_auth_key.set()
    prompt = (
        f"📋 <b>Привязка нового клиента</b>\n"
        f"<b>Virtual raqam:</b> <code>{html.escape(str(virtual_number))}</code>\n"
        f"<b>Fiskal raqam:</b> <code>{html.escape(str(new_fiscal))}</code>\n"
        f"<b>Diller:</b> <code>{html.escape(str(diller_name))}</code>\n"
        f"<b>Firma nomi:</b> <code>{html.escape(str(organization))}</code>\n"
        f"<b>Faoliyat turi:</b> <code>{html.escape(str(activity_type))}</code>\n\n"
        f"Отправьте <b>auth key</b>:"
    )
    await save_prompt(state, prompt)
    await message.answer(prompt)


@dp.message_handler(
    chat_type=types.ChatType.PRIVATE,
    content_types=types.ContentType.TEXT,
    state=NewClientClaim.waiting_for_auth_key,
)
async def receive_new_client_auth_key(message: types.Message, state: FSMContext):
    auth_key = message.text.strip()
    if not auth_key:
        await prompt_continue_or_exit(
            message, state, hint="auth key не может быть пустым."
        )
        return

    await state.update_data(nc_auth_key=auth_key)
    await NewClientClaim.confirming.set()

    data = await state.get_data()
    organization = data.get("nc_organization") or "—"
    stir = data.get("nc_stir") or "—"
    address = data.get("nc_address") or "—"
    new_fiscal = data.get("nc_new_fiscal") or "—"
    activity_type = data.get("nc_activity_type") or "—"
    diller_name = data.get("nc_diller_name") or "—"
    blocked_date = _calc_blocked_date()

    prompt = (
        f"<b>Бизнес будет добавлен:</b>\n"
        f"<b>Fiskal raqam:</b> <code>{html.escape(str(new_fiscal))}</code>\n"
        f"<b>Auth key:</b> <code>{html.escape(auth_key)}</code>\n"
        f"<b>Diller:</b> <code>{html.escape(str(diller_name))}</code>\n"
        f"<b>STIR:</b> <code>{html.escape(str(stir))}</code>\n"
        f"<b>Faoliyat turi:</b> <code>{html.escape(str(activity_type))}</code>\n"
        f"<b>Block kuni:</b> <code>{html.escape(blocked_date)}</code>\n"
        f"<b>Biznes nomi:</b> <code>{html.escape(str(organization))}</code>\n"
        f"<b>Tashkilot nomi:</b> <code>{html.escape(str(organization))}</code>\n"
        f"<b>Manzil:</b> <code>{html.escape(str(address))}</code>"
    )
    await save_prompt(state, prompt)
    await message.answer(prompt, reply_markup=new_client_confirm_keyboard())


@dp.callback_query_handler(
    new_client_cb.filter(action="resend"),
    state=NewClientClaim.confirming,
)
async def new_client_resend(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(nc_auth_key=None)
    await NewClientClaim.waiting_for_auth_key.set()
    prompt = "Отправьте <b>auth key</b> заново:"
    await save_prompt(state, prompt)
    await callback.message.edit_text(prompt)
    await callback.answer()


@dp.callback_query_handler(
    new_client_cb.filter(action="confirm"),
    state=NewClientClaim.confirming,
)
async def new_client_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    business_id = data.get("nc_business_id")
    business = data.get("nc_business") or {}
    organization = data.get("nc_organization")
    address = data.get("nc_address")
    stir = data.get("nc_stir")
    auth_key = data.get("nc_auth_key")
    user_diller_id = data.get("nc_user_diller_id")
    new_fiscal = data.get("nc_new_fiscal")
    business_type = data.get("nc_business_type")

    if not all([business_id, new_fiscal, auth_key, user_diller_id]):
        await callback.message.edit_text("⚠️ Состояние утеряно, отправьте PDF заново.")
        await state.finish()
        await callback.answer()
        return

    # update_business — перезаписываем нужные поля, остальные сохраняем
    payload = {
        key: _flatten_fk(business.get(key))
        for key in UPDATABLE_FIELDS
        if key in business
    }
    payload["name"] = new_fiscal
    payload["diller"] = user_diller_id
    payload["auth_key"] = auth_key
    payload["TIN"] = stir
    payload["pinfl_tin"] = stir
    payload["blocked_date"] = _calc_blocked_date()
    if business_type is not None:
        payload["business_type"] = business_type

    try:
        token = await epos_api.get_token()
        await update_business(business_id, token, **payload)
    except EposAPIError as e:
        await callback.message.edit_text(
            f"⚠️ update_business: {html.escape(str(e))}"
        )
        await state.finish()
        await callback.answer()
        return

    # Branch: если есть — апдейтим, если нет — создаём через POST /v1/branches/
    branches = business.get("branches") or []
    target = next(
        (b for b in branches if isinstance(b, dict) and b.get("id")), None
    )

    if target:
        branch_id = target["id"]
        branch_payload = {
            key: _flatten_fk(target.get(key))
            for key in BRANCH_UPDATABLE_FIELDS
            if key in target
        }
        branch_payload["name"] = organization
        branch_payload["address"] = address
        branch_payload["contact_person"] = "User"
        branch_payload["contact_phone"] = "+998"
        branch_payload["business"] = business_id

        try:
            await update_branch(branch_id, token, **branch_payload)
        except EposAPIError as e:
            await callback.message.edit_text(
                f"⚠️ update_branch: {html.escape(str(e))}\n"
                f"(business уже обновлён)"
            )
            await state.finish()
            await callback.answer()
            return
    else:
        try:
            await create_branch(
                token,
                name=organization,
                address=address,
                contact_person="User",
                contact_phone="+998",
                business=business_id,
                city=None,
            )
        except EposAPIError as e:
            await callback.message.edit_text(
                f"⚠️ create_branch: {html.escape(str(e))}\n"
                f"(business уже обновлён)"
            )
            await state.finish()
            await callback.answer()
            return

    await callback.message.edit_text("✅ Новый клиент добавлен.")

    # Только теперь, после успешного добавления в Cazad, отправляем PDF +
    # анализ пользователю в личку и дублируем в лог-группы этого дилера.
    doc_file_id = data.get("nc_doc_file_id")
    analysis_text = data.get("nc_analysis_text")
    diller_name = data.get("nc_diller_name")
    if doc_file_id and analysis_text:
        try:
            await _send_pdf_to_user_and_group(
                callback.message.chat.id,
                callback.from_user,
                doc_file_id,
                analysis_text,
                diller_name=diller_name,
                diller_id=user_diller_id,
            )
        except Exception as e:
            logging.exception(f"failed to resend PDF after new-client success: {e}")

    # Сводка в лог-группы дилера (текстом, помимо PDF выше).
    user = callback.from_user
    summary = (
        f"🆕 <b>Новый клиент добавлен</b>\n"
        f"<b>Diller:</b> {html.escape(str(diller_name or '—'))}\n"
        f'От: <a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a> '
        f"(id: <code>{user.id}</code>)\n\n"
        f"<b>Fiskal raqam:</b> <code>{html.escape(str(new_fiscal))}</code>\n"
        f"<b>STIR:</b> <code>{html.escape(str(stir))}</code>\n"
        f"<b>Tashkilot:</b> <code>{html.escape(str(organization))}</code>\n"
        f"<b>Manzil:</b> <code>{html.escape(str(address))}</code>"
    )
    await notify_log_groups(user_diller_id, summary)

    await state.finish()
    await callback.answer()


async def _start_fiscal_change_flow(
    message: types.Message,
    state: FSMContext,
    parsed: dict,
    business: dict,
    business_id,
):
    fiscal_modules = parsed.get("fiskal_modules") or []
    if len(fiscal_modules) < 2:
        return

    new_fiscal = fiscal_modules[-1]
    old_fiscal = fiscal_modules[-2]
    api_name = business.get("name")

    if api_name == new_fiscal:
        await message.answer("ℹ️ Этот фискальный модуль уже внесён в базу.")
        return

    if api_name != old_fiscal:
        await message.answer(
            f"⚠️ Старый фискальный модуль не совпадает с name в базе.\n"
            f"PDF (старый): <code>{html.escape(str(old_fiscal))}</code>\n"
            f"База (name): <code>{html.escape(str(api_name))}</code>"
        )
        return

    await state.update_data(
        fiscal_business_id=business_id,
        fiscal_business=business,
        fiscal_old=old_fiscal,
        fiscal_new=new_fiscal,
    )
    await FiscalModule.waiting_for_factory_id.set()
    prompt = (
        f"📋 <b>Замена фискального модуля</b>\n"
        f"Старый: <code>{html.escape(str(old_fiscal))}</code>\n"
        f"Новый: <code>{html.escape(str(new_fiscal))}</code>\n\n"
        f"Отправьте <b>auth key</b>:"
    )
    await save_prompt(state, prompt)
    await message.answer(prompt)


@dp.message_handler(
    chat_type=types.ChatType.PRIVATE,
    content_types=types.ContentType.TEXT,
    state=FiscalModule.waiting_for_factory_id,
)
async def receive_factory_id(message: types.Message, state: FSMContext):
    factory_id = message.text.strip()
    if not factory_id:
        await prompt_continue_or_exit(
            message, state, hint="auth key не может быть пустым."
        )
        return

    await state.update_data(factory_id=factory_id)
    await FiscalModule.confirming.set()

    data = await state.get_data()
    new_fiscal = data.get("fiscal_new", "")

    prompt = (
        f"factory_id: <code>{html.escape(factory_id)}</code>\n"
        f"Новое <b>name</b>: <code>{html.escape(str(new_fiscal))}</code>\n"
        f"Новый <b>auth_key</b>: <code>{html.escape(factory_id)}</code>"
    )
    await save_prompt(state, prompt)
    await message.answer(prompt, reply_markup=fiscal_confirm_keyboard())


@dp.callback_query_handler(
    fiscal_cb.filter(action="resend"),
    state=FiscalModule.confirming,
)
async def fiscal_resend(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(factory_id=None)
    await FiscalModule.waiting_for_factory_id.set()
    prompt = "Отправьте <b>auth key</b> заново:"
    await save_prompt(state, prompt)
    await callback.message.edit_text(prompt)
    await callback.answer()


@dp.callback_query_handler(
    fiscal_cb.filter(action="confirm"),
    state=FiscalModule.confirming,
)
async def fiscal_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    business_id = data.get("fiscal_business_id")
    business = data.get("fiscal_business") or {}
    new_fiscal = data.get("fiscal_new", "")
    factory_id = data.get("factory_id", "")

    if not business_id or not factory_id:
        await callback.message.edit_text("⚠️ Состояние утеряно, отправь PDF заново.")
        await state.finish()
        await callback.answer()
        return

    payload = {
        key: _flatten_fk(business.get(key))
        for key in UPDATABLE_FIELDS
        if key in business
    }
    payload["name"] = new_fiscal
    payload["auth_key"] = factory_id

    try:
        token = await epos_api.get_token()
        await update_business(business_id, token, **payload)
    except EposAPIError as e:
        await callback.message.edit_text(
            f"⚠️ update_business: {html.escape(str(e))}"
        )
        await state.finish()
        await callback.answer()
        return

    await callback.message.edit_text(
        f"✅ Business обновлён.\n"
        f"<b>name:</b> <code>{html.escape(str(new_fiscal))}</code>\n"
        f"<b>auth_key:</b> <code>{html.escape(factory_id)}</code>"
    )

    # В лог-группы дилера, владеющего этим бизнесом.
    business_diller_id = _flatten_fk(business.get("diller"))
    user = callback.from_user
    diller_name = await get_user_diller_name(user.id) or "—"
    summary = (
        f"🔁 <b>Заменён фискальный модуль</b>\n"
        f"<b>Diller:</b> {html.escape(str(diller_name))}\n"
        f'От: <a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a> '
        f"(id: <code>{user.id}</code>)\n\n"
        f"<b>Новый name:</b> <code>{html.escape(str(new_fiscal))}</code>\n"
        f"<b>Новый auth_key:</b> <code>{html.escape(factory_id)}</code>"
    )
    await notify_log_groups(business_diller_id, summary)

    await state.finish()
    await callback.answer()


async def _start_address_change_flow(
    message: types.Message,
    parsed: dict,
    business: dict,
    business_id,
):
    new_address = parsed.get("address")
    if not new_address or new_address == "—":
        await message.answer("⚠️ Адрес в PDF не найден.")
        return

    branches = business.get("branches") or []
    if not isinstance(branches, list) or not branches:
        await message.answer("⚠️ У business нет branches для обновления.")
        return

    new_norm = new_address.strip().lower()
    for b in branches:
        if not isinstance(b, dict):
            continue
        api_addr = (b.get("address") or "").strip().lower()
        if api_addr and api_addr == new_norm:
            await message.answer("ℹ️ Адрес уже изменён в базе.")
            return

    target = next(
        (b for b in branches if isinstance(b, dict) and b.get("id")), None
    )
    if not target:
        await message.answer("⚠️ Не нашёл branch для обновления.")
        return

    branch_id = target["id"]
    payload = {
        key: _flatten_fk(target.get(key))
        for key in BRANCH_UPDATABLE_FIELDS
        if key in target
    }
    payload["address"] = new_address
    payload["business"] = business_id

    try:
        token = await epos_api.get_token()
        await update_branch(branch_id, token, **payload)
    except EposAPIError as e:
        logging.exception("update_branch failed")
        await message.answer(f"⚠️ update_branch: {html.escape(str(e))}")
        return

    await message.answer(
        f"✅ Адрес обновлён.\n"
        f"<b>Новый адрес:</b> <code>{html.escape(str(new_address))}</code>"
    )

    business_diller_id = _flatten_fk(business.get("diller"))
    user = message.from_user
    diller_name = await get_user_diller_name(user.id) or "—"
    summary = (
        f"📍 <b>Адрес обновлён</b>\n"
        f"<b>Diller:</b> {html.escape(str(diller_name))}\n"
        f'От: <a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a> '
        f"(id: <code>{user.id}</code>)\n\n"
        f"<b>Новый адрес:</b> <code>{html.escape(str(new_address))}</code>"
    )
    await notify_log_groups(business_diller_id, summary)


async def _send_doc_with_text(chat_id, document, text: str) -> None:
    """Send document with text as caption; fall back to two messages if over limit."""
    if len(text) <= CAPTION_LIMIT:
        await bot.send_document(chat_id=chat_id, document=document, caption=text)
    else:
        await bot.send_document(chat_id=chat_id, document=document)
        await bot.send_message(chat_id, text)


@dp.message_handler(
    state=FiscalModule.waiting_for_factory_id,
    content_types=types.ContentType.ANY,
)
async def fiscal_factory_id_fallback(message: types.Message, state: FSMContext):
    # Текст обработан receive_factory_id (зарегистрирован выше).
    if message.content_type == types.ContentType.TEXT:
        return
    await prompt_continue_or_exit(
        message, state, hint="Ожидается auth key (текст)."
    )


@dp.message_handler(
    state=FiscalModule.confirming,
    content_types=types.ContentType.ANY,
)
async def fiscal_confirm_fallback(message: types.Message, state: FSMContext):
    await prompt_continue_or_exit(
        message, state, hint="Нажми ✅ Подтвердить или 🔄 Отправить заново."
    )


@dp.message_handler(
    state=NewClientClaim.waiting_for_auth_key,
    content_types=types.ContentType.ANY,
)
async def new_client_auth_key_fallback(message: types.Message, state: FSMContext):
    if message.content_type == types.ContentType.TEXT:
        return
    await prompt_continue_or_exit(
        message, state, hint="Ожидается auth key (текст)."
    )


@dp.message_handler(
    state=NewClientClaim.confirming,
    content_types=types.ContentType.ANY,
)
async def new_client_confirm_fallback(message: types.Message, state: FSMContext):
    await prompt_continue_or_exit(
        message, state, hint="Нажми ✅ Подтвердить или 🔄 Отправить заново."
    )
