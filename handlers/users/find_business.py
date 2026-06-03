import html
import logging

from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram_calendar import SimpleCalendar, simple_cal_callback

from data import config
from keyboards.default.admin import (
    ADD_DILLER_BTN,
    ADD_VIRTUAL_NUMBERS_BTN,
    ATTACH_BUSINESS_BTN,
)
from loader import bot, db, dp
from utils.diller import get_user_diller_name
from utils.epos_api import EposAPIError, authed_http, epos_api
from utils.state_control import prompt_continue_or_exit, save_prompt

# Тексты reply-кнопок, которые не должны трактоваться как фискальный номер.
_BUTTON_TEXTS = {ADD_VIRTUAL_NUMBERS_BTN, ADD_DILLER_BTN, ATTACH_BUSINESS_BTN}

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


class FindBusiness(StatesGroup):
    waiting_for_date = State()


async def get_business_by_name(name: str, token: str):
    """GET /v1/all-business/?name=... — auto-retry on 401 via authed_http."""
    url = f"{config.EPOS_API_URL.rstrip('/')}/v1/all-business/?name={name}"
    return await authed_http("GET", url, token)


async def update_business(business_id, token, **fields):
    """PUT /v1/businesses/{business_id}/ — auto-retry on 401 via authed_http."""
    url = f"{config.EPOS_API_URL.rstrip('/')}/v1/businesses/{business_id}/"
    return await authed_http("PUT", url, token, json=fields)


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
    """API GET'ом отдаёт FK-поля как вложенные объекты ({"id": ...}),
    а PUT ждёт pk. Сводим dict к его id / pk."""
    if isinstance(value, dict):
        return value.get("id") or value.get("pk")
    return value


def _branch_text(business: dict) -> str:
    branches = business.get("branches") or []
    if not isinstance(branches, list):
        return "—"
    names = [
        str(b["name"])
        for b in branches
        if isinstance(b, dict) and b.get("name")
    ]
    return ", ".join(names) if names else "—"


def _summary(name, tin, branch, blocked_date) -> str:
    return (
        f"<b>Фискальный номер:</b> <code>{html.escape(str(name))}</code>\n"
        f"<b>ИНН:</b> <code>{html.escape(str(tin))}</code>\n"
        f"<b>Название бизнеса:</b> <code>{html.escape(str(branch))}</code>\n"
        f"<b>Дата блокировки:</b> <code>{html.escape(str(blocked_date))}</code>"
    )


@dp.message_handler(
    lambda m: (
        m.text
        and not m.text.startswith("/")
        and m.text not in _BUTTON_TEXTS
    ),
    chat_type=types.ChatType.PRIVATE,
    content_types=types.ContentType.TEXT,
    state=None,
)
async def find_business_by_name(message: types.Message, state: FSMContext):
    name = message.text.strip()

    diller_ids = await db.get_diller_ids_by_chat_id(message.from_user.id)
    if not diller_ids:
        await message.answer("⛔ Эта функция вам недоступна.")
        return

    try:
        token = await epos_api.get_token()
        response = await get_business_by_name(name, token)
    except EposAPIError as e:
        await message.answer(f"⚠️ get_business_by_name: {html.escape(str(e))}")
        return

    business = _pick_business(response)
    business_id = business.get("id") or business.get("business_id")
    if not business_id:
        await message.answer(
            f"⚠️ Бизнес с name=<code>{html.escape(name)}</code> не найден."
        )
        return

    business_diller_id = _flatten_fk(business.get("diller"))
    if business_diller_id not in diller_ids:
        await message.answer("⛔ Этот клиент вам не принадлежит.")
        return

    tin = business.get("TIN") or business.get("tin") or "—"
    branch = _branch_text(business)
    current_blocked = business.get("blocked_date") or "—"

    text = (
        f"{_summary(name, tin, branch, current_blocked)}\n\n"
        f"Выберите дату блокировки:"
    )

    await state.update_data(
        name=name,
        business=business,
        business_id=business_id,
        tin=tin,
        branch=branch,
    )
    await FindBusiness.waiting_for_date.set()
    await save_prompt(state, text)
    await message.answer(
        text,
        reply_markup=await SimpleCalendar().start_calendar(),
    )


@dp.callback_query_handler(
    simple_cal_callback.filter(),
    state=FindBusiness.waiting_for_date,
)
async def pick_business_date(
    callback: types.CallbackQuery,
    callback_data: dict,
    state: FSMContext,
):
    selected, picked = await SimpleCalendar().process_selection(
        callback, callback_data
    )
    if not selected:
        return

    data = await state.get_data()
    name = data.get("name", "—")
    tin = data.get("tin", "—")
    branch = data.get("branch", "—")
    business = data.get("business") or {}
    business_id = data.get("business_id")
    blocked_date = picked.strftime("%Y-%m-%d")

    payload = {
        key: _flatten_fk(business.get(key))
        for key in UPDATABLE_FIELDS
        if key in business
    }
    payload["blocked_date"] = blocked_date

    try:
        token = await epos_api.get_token()
        await update_business(business_id, token, **payload)
    except EposAPIError as e:
        await callback.message.edit_text(
            f"⚠️ update_business: {html.escape(str(e))}"
        )
        await state.finish()
        return

    await callback.message.edit_text(
        "✅ Business успешно обновлён\n\n"
        + _summary(name, tin, branch, blocked_date)
    )

    if config.PDF_GROUP_CHAT_ID:
        user = callback.from_user
        full_name = html.escape(user.full_name)
        diller_name = await get_user_diller_name(user.id) or "—"
        group_text = (
            f"🔒 <b>Обновлена дата блокировки</b>\n"
            f"<b>Diller:</b> {html.escape(str(diller_name))}\n"
            f'От: <a href="tg://user?id={user.id}">{full_name}</a> '
            f"(id: <code>{user.id}</code>)\n\n"
            + _summary(name, tin, branch, blocked_date)
        )
        try:
            await bot.send_message(
                config.PDF_GROUP_CHAT_ID, group_text, disable_web_page_preview=True
            )
        except Exception as e:
            logging.exception(f"failed to notify PDF group: {e}")

    await state.finish()


@dp.message_handler(
    state=FindBusiness.waiting_for_date,
    content_types=types.ContentType.ANY,
)
async def find_business_date_fallback(message: types.Message, state: FSMContext):
    await prompt_continue_or_exit(
        message, state, hint="Выбери дату в календаре."
    )
