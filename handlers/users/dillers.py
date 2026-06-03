import html
import re

from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

from data import config
from data.config import ADMINS
from keyboards.default.admin import ADD_DILLER_BTN
from keyboards.inline.dillers import diller_cb, dillers_keyboard
from loader import db, dp
from utils.epos_api import EposAPIError, authed_http, epos_api
from utils.state_control import prompt_continue_or_exit, save_prompt


class AddDiller(StatesGroup):
    choosing_diller = State()
    waiting_for_chat_id = State()


async def get_dillers(token: str):
    """GET /v1/diller/ — auto-retry on 401 via authed_http."""
    url = f"{config.EPOS_API_URL.rstrip('/')}/v1/diller/"
    return await authed_http("GET", url, token)


def _pick_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "data", "dillers"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
    return []


@dp.message_handler(
    lambda m: str(m.from_user.id) in ADMINS,
    chat_type=types.ChatType.PRIVATE,
    text=ADD_DILLER_BTN,
    state="*",
)
async def start_add_diller(message: types.Message, state: FSMContext):
    try:
        token = await epos_api.get_token()
        payload = await get_dillers(token)
    except EposAPIError as e:
        await message.answer(f"⚠️ get_dillers: {html.escape(str(e))}")
        return

    dillers = _pick_list(payload)
    if not dillers:
        await message.answer("⚠️ Список дилеров пустой.")
        return

    by_id = {}
    for d in dillers:
        if not isinstance(d, dict):
            continue
        did = d.get("id") or d.get("pk")
        if did is None:
            continue
        by_id[str(did)] = {
            "id": did,
            "name": d.get("name") or "—",
            "inn": d.get("inn"),
            "phone_number": d.get("phone_number"),
            "address": d.get("address"),
            "responsible_person": d.get("responsible_person"),
        }
    await state.update_data(dillers_by_id=by_id)
    await AddDiller.choosing_diller.set()
    prompt = "Выберите дилера:"
    await save_prompt(state, prompt)
    await message.answer(prompt, reply_markup=dillers_keyboard(dillers))


@dp.callback_query_handler(
    diller_cb.filter(),
    state=AddDiller.choosing_diller,
)
async def diller_chosen(
    callback: types.CallbackQuery,
    callback_data: dict,
    state: FSMContext,
):
    diller_id_str = callback_data["diller_id"]
    data = await state.get_data()
    by_id = data.get("dillers_by_id") or {}
    diller = by_id.get(diller_id_str)
    if not diller:
        await callback.answer("Дилер не найден", show_alert=True)
        await state.finish()
        return

    try:
        diller_id = int(diller_id_str)
    except ValueError:
        await callback.answer("Некорректный id", show_alert=True)
        await state.finish()
        return

    diller_name = diller.get("name") or "—"
    await state.update_data(
        diller_id=diller_id,
        diller_name=diller_name,
        diller=diller,
    )
    await AddDiller.waiting_for_chat_id.set()
    prompt = (
        f"Дилер: <b>{html.escape(diller_name)}</b>\n\n"
        f"Отправьте chat_id. Можно несколько — через пробел, запятую или с новой строки."
    )
    await save_prompt(state, prompt)
    await callback.message.edit_text(prompt)
    await callback.answer()


@dp.message_handler(
    state=AddDiller.waiting_for_chat_id,
    content_types=types.ContentType.TEXT,
)
async def receive_chat_ids(message: types.Message, state: FSMContext):
    tokens = [t for t in re.split(r"[\s,;]+", message.text.strip()) if t]
    chat_ids = []
    bad = []
    for t in tokens:
        try:
            chat_ids.append(int(t))
        except ValueError:
            bad.append(t)

    if bad:
        await prompt_continue_or_exit(
            message,
            state,
            hint=(
                "Не распознано как chat_id: "
                f"{html.escape(', '.join(bad))}. Только числа."
            ),
        )
        return

    if not chat_ids:
        await prompt_continue_or_exit(
            message, state, hint="Не нашёл ни одного chat_id."
        )
        return

    data = await state.get_data()
    diller_id = data.get("diller_id")
    diller_name = data.get("diller_name") or "—"
    diller = data.get("diller") or {}

    await db.upsert_diller(
        diller_id=diller_id,
        name=diller_name,
        inn=diller.get("inn"),
        phone_number=diller.get("phone_number"),
        address=diller.get("address"),
        responsible_person=diller.get("responsible_person"),
    )

    saved = []
    for cid in chat_ids:
        await db.add_diller_chat(diller_id, diller_name, cid)
        saved.append(cid)

    lines = "\n".join(f"<code>{c}</code>" for c in saved)
    await message.answer(
        f"✅ Сохранено для <b>{html.escape(diller_name)}</b> "
        f"(diller_id=<code>{diller_id}</code>):\n{lines}"
    )
    await state.finish()


@dp.message_handler(
    state=AddDiller.choosing_diller,
    content_types=types.ContentType.ANY,
)
async def add_diller_choose_fallback(message: types.Message, state: FSMContext):
    await prompt_continue_or_exit(
        message, state, hint="Выбери дилера кнопкой выше."
    )


@dp.message_handler(
    state=AddDiller.waiting_for_chat_id,
    content_types=types.ContentType.ANY,
)
async def add_diller_chat_fallback(message: types.Message, state: FSMContext):
    await prompt_continue_or_exit(
        message, state, hint="Ожидаются chat_id (числа)."
    )
