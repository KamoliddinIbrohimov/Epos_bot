import html
import io
import logging

from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

from data.config import ADMINS
from handlers.users.business import (
    UPDATABLE_FIELDS,
    _flatten_fk,
    _pick_business,
    update_business,
)
from handlers.users.dillers import _pick_list, get_dillers
from keyboards.default.admin import ATTACH_BUSINESS_BTN
from keyboards.inline.dillers import attach_diller_cb, attach_dillers_keyboard
from loader import dp
from utils.epos_api import EposAPIError, epos_api
from utils.state_control import prompt_continue_or_exit, save_prompt


class AttachBusiness(StatesGroup):
    choosing_diller = State()
    waiting_for_xlsx = State()


@dp.message_handler(
    lambda m: str(m.from_user.id) in ADMINS,
    chat_type=types.ChatType.PRIVATE,
    text=ATTACH_BUSINESS_BTN,
    state="*",
)
async def start_attach_business(message: types.Message, state: FSMContext):
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

    by_id = {
        str(d.get("id") or d.get("pk")): str(d.get("name") or "—")
        for d in dillers
        if isinstance(d, dict) and (d.get("id") or d.get("pk")) is not None
    }
    await state.update_data(attach_dillers_by_id=by_id)
    await AttachBusiness.choosing_diller.set()
    prompt = "Выберите дилера:"
    await save_prompt(state, prompt)
    await message.answer(prompt, reply_markup=attach_dillers_keyboard(dillers))


@dp.callback_query_handler(
    attach_diller_cb.filter(),
    state=AttachBusiness.choosing_diller,
)
async def attach_diller_picked(
    callback: types.CallbackQuery,
    callback_data: dict,
    state: FSMContext,
):
    diller_id_str = callback_data["diller_id"]
    data = await state.get_data()
    by_id = data.get("attach_dillers_by_id") or {}
    diller_name = by_id.get(diller_id_str)
    if not diller_name:
        await callback.answer("Дилер не найден", show_alert=True)
        await state.finish()
        return

    try:
        diller_id = int(diller_id_str)
    except ValueError:
        await callback.answer("Некорректный id", show_alert=True)
        await state.finish()
        return

    await state.update_data(
        attach_diller_id=diller_id,
        attach_diller_name=diller_name,
    )
    await AttachBusiness.waiting_for_xlsx.set()
    prompt = (
        f"Дилер: <b>{html.escape(diller_name)}</b>\n\n"
        f"Отправьте <b>.xlsx</b> файл."
    )
    await save_prompt(state, prompt)
    await callback.message.edit_text(prompt)
    await callback.answer()


@dp.message_handler(
    lambda m: str(m.from_user.id) in ADMINS,
    chat_type=types.ChatType.PRIVATE,
    content_types=types.ContentType.DOCUMENT,
    state=AttachBusiness.waiting_for_xlsx,
)
async def attach_receive_xlsx(message: types.Message, state: FSMContext):
    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".xlsx"):
        await prompt_continue_or_exit(
            message, state, hint="Нужен именно .xlsx файл."
        )
        return

    data = await state.get_data()
    diller_id = data.get("attach_diller_id")
    diller_name = data.get("attach_diller_name") or "—"

    if diller_id is None:
        await message.answer("⚠️ Состояние утеряно. Нажмите кнопку заново.")
        await state.finish()
        return

    buffer = io.BytesIO()
    try:
        await doc.download(destination_file=buffer)
    except Exception as e:
        logging.exception("xlsx download failed")
        await message.answer(f"⚠️ Не удалось скачать файл: {html.escape(str(e))}")
        await state.finish()
        return
    buffer.seek(0)

    try:
        import openpyxl  # lazy: модуль может быть не установлен — отвечаем понятной ошибкой
    except ImportError:
        await message.answer(
            "⚠️ В окружении нет пакета <b>openpyxl</b>. Установи его "
            "(<code>pip install openpyxl</code>) и перезапусти бот."
        )
        await state.finish()
        return

    try:
        wb = openpyxl.load_workbook(buffer, data_only=True, read_only=True)
        sheet = wb.active
        fiscal_numbers = []
        for row in sheet.iter_rows(values_only=True):
            if not row:
                continue
            cell = row[0]
            if cell is None:
                continue
            value = str(cell).strip()
            if value:
                fiscal_numbers.append(value)
        wb.close()
    except Exception as e:
        logging.exception("xlsx parse failed")
        await message.answer(
            f"⚠️ Не удалось прочитать .xlsx: {html.escape(str(e))}"
        )
        await state.finish()
        return

    if not fiscal_numbers:
        await message.answer("⚠️ В колонке A файла нет значений.")
        await state.finish()
        return

    progress = await message.answer(
        f"⏳ Обработка {len(fiscal_numbers)} фискальных номеров…"
    )

    try:
        token = await epos_api.get_token()
    except EposAPIError as e:
        await progress.edit_text(f"⚠️ get_token: {html.escape(str(e))}")
        await state.finish()
        return

    # Импортируем лениво — иначе модуль find_business загружается при импорте
    # attach_business и его catch-all text-handler регистрируется раньше, чем
    # хендлер кнопки ATTACH_BUSINESS_BTN, и перехватывает нажатие.
    from handlers.users.find_business import get_business_by_name

    attached = []
    skipped_other = []
    skipped_already = []
    not_found = []
    errors = []

    for fn in fiscal_numbers:
        try:
            response = await get_business_by_name(fn, token)
        except EposAPIError as e:
            errors.append((fn, str(e)))
            continue

        business = _pick_business(response)
        business_id = business.get("id") or business.get("business_id")
        if not business_id:
            not_found.append(fn)
            continue

        current_diller = _flatten_fk(business.get("diller"))

        # Если привязан к другому дилеру — пропускаем без изменений.
        if current_diller is not None and current_diller != diller_id:
            skipped_other.append(fn)
            continue

        # Уже привязан к этому же дилеру — ничего не делаем.
        if current_diller == diller_id:
            skipped_already.append(fn)
            continue

        payload = {
            key: _flatten_fk(business.get(key))
            for key in UPDATABLE_FIELDS
            if key in business
        }
        payload["diller"] = diller_id

        try:
            await update_business(business_id, token, **payload)
            attached.append(fn)
        except EposAPIError as e:
            errors.append((fn, str(e)))

    lines = [
        f"✅ Готово. Дилер: <b>{html.escape(diller_name)}</b>",
        f"Всего: <b>{len(fiscal_numbers)}</b>",
        f"Привязано: <b>{len(attached)}</b>",
    ]
    if skipped_already:
        lines.append(f"Уже у этого дилера: {len(skipped_already)}")
    if skipped_other:
        lines.append(f"Принадлежат другому дилеру: {len(skipped_other)}")
    if not_found:
        lines.append(f"Не найдено в Cazad: {len(not_found)}")
    if errors:
        lines.append(f"Ошибки: {len(errors)}")

    detail_parts = []
    if skipped_other:
        sample = ", ".join(f"<code>{html.escape(x)}</code>" for x in skipped_other[:5])
        detail_parts.append(f"\n<b>Другой дилер:</b> {sample}")
    if not_found:
        sample = ", ".join(f"<code>{html.escape(x)}</code>" for x in not_found[:5])
        detail_parts.append(f"\n<b>Не найдены:</b> {sample}")
    if errors:
        sample = ", ".join(
            f"<code>{html.escape(fn)}</code>" for fn, _ in errors[:5]
        )
        detail_parts.append(f"\n<b>Ошибки:</b> {sample}")

    try:
        await progress.edit_text("\n".join(lines) + "".join(detail_parts))
    except Exception:
        await message.answer("\n".join(lines) + "".join(detail_parts))

    await state.finish()


@dp.message_handler(
    state=AttachBusiness.choosing_diller,
    content_types=types.ContentType.ANY,
)
async def attach_choose_fallback(message: types.Message, state: FSMContext):
    await prompt_continue_or_exit(
        message, state, hint="Выбери дилера кнопкой выше."
    )


@dp.message_handler(
    state=AttachBusiness.waiting_for_xlsx,
    content_types=types.ContentType.ANY,
)
async def attach_xlsx_fallback(message: types.Message, state: FSMContext):
    # Документ обрабатывается attach_receive_xlsx, регистрируется выше.
    # Сюда попадают любые не-документы.
    if message.content_type == types.ContentType.DOCUMENT:
        return
    await prompt_continue_or_exit(
        message, state, hint="Ожидается .xlsx файл."
    )
