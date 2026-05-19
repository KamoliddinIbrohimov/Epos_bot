import io
from datetime import date, datetime

import xlwt
from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

from data.config import ADMINS
from keyboards.default.admin import ADD_VIRTUAL_NUMBERS_BTN
from keyboards.inline.virtual_numbers import (
    virtual_numbers_cb,
    virtual_numbers_confirm_keyboard,
)
from loader import dp
from utils.epos_api import EposAPIError, epos_api

MAX_VIRTUAL_NUMBERS = 2000

NEW_BUSINESS_PAYLOAD = {
    "name": " ",
    "auth_key": " ",
    "virtual_number": 0,
    "tin": "-",
    "version_info": " ",
    "status": True,
    "expire_date": date.today().isoformat(),
}


class AddVirtualNumbers(StatesGroup):
    waiting_for_count = State()
    confirming = State()


@dp.message_handler(
    lambda m: str(m.from_user.id) in ADMINS,
    chat_type=types.ChatType.PRIVATE,
    text=ADD_VIRTUAL_NUMBERS_BTN,
    state="*",
)
async def ask_virtual_numbers_count(message: types.Message, state: FSMContext):
    await AddVirtualNumbers.waiting_for_count.set()
    await message.answer(
        "Сколько виртуальных номеров создать? Отправь число "
        f"(от 1 до {MAX_VIRTUAL_NUMBERS})."
    )


@dp.message_handler(
    lambda m: str(m.from_user.id) in ADMINS,
    chat_type=types.ChatType.PRIVATE,
    content_types=types.ContentType.TEXT,
    state=AddVirtualNumbers.waiting_for_count,
)
async def receive_virtual_numbers_count(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    try:
        count = int(raw)
    except ValueError:
        await message.answer("⚠️ Введи целое число.")
        return
    if count <= 0:
        await message.answer("⚠️ Число должно быть больше 0.")
        return
    if count > MAX_VIRTUAL_NUMBERS:
        await message.answer(
            f"⚠️ Слишком много. Максимум {MAX_VIRTUAL_NUMBERS} за раз."
        )
        return

    await state.update_data(vn_count=count)
    await AddVirtualNumbers.confirming.set()
    await message.answer(
        f"Запросить <b>{count}</b> виртуальных номеров?",
        reply_markup=virtual_numbers_confirm_keyboard(),
    )


@dp.callback_query_handler(
    virtual_numbers_cb.filter(action="cancel"),
    lambda c: str(c.from_user.id) in ADMINS,
    state=AddVirtualNumbers.confirming,
)
async def cancel_virtual_numbers(call: types.CallbackQuery, state: FSMContext):
    try:
        await call.message.edit_text("❌ Действие отменено.")
    except Exception:
        pass
    await state.finish()
    await call.answer("Отменено.")


@dp.callback_query_handler(
    virtual_numbers_cb.filter(action="confirm"),
    lambda c: str(c.from_user.id) in ADMINS,
    state=AddVirtualNumbers.confirming,
)
async def add_virtual_numbers(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    count = data.get("vn_count")
    await state.finish()

    if not count or count <= 0:
        await call.answer("Состояние утеряно, нажми кнопку заново.", show_alert=True)
        return

    await call.answer()
    progress = await call.message.edit_text(
        f"Запрашиваю {count} виртуальных номеров..."
    )

    numbers = []
    try:
        for i in range(count):
            response = await epos_api.request(
                "POST",
                "/billing/api/v3/business/",
                json=NEW_BUSINESS_PAYLOAD,
            )
            virtual_number = _extract_virtual_number(response)
            if virtual_number is None:
                await progress.edit_text(
                    f"Запрос {i + 1}/{count} не вернул virtual_number. "
                    f"Ответ: {response}"
                )
                return
            numbers.append(virtual_number)
    except EposAPIError as e:
        await progress.edit_text(f"API вернул ошибку: {e}")
        return

    xls_buffer = _build_xls(numbers)
    file_name = f"virtual_numbers_{datetime.now():%Y%m%d_%H%M%S}.xls"

    await progress.delete()
    await call.message.answer_document(
        types.InputFile(xls_buffer, filename=file_name),
        caption=f"Получено {len(numbers)} виртуальных номеров",
    )


def _extract_virtual_number(response):
    if isinstance(response, dict):
        return response.get("virtual_number")
    return None


def _build_xls(numbers) -> io.BytesIO:
    workbook = xlwt.Workbook(encoding="utf-8")
    sheet = workbook.add_sheet("Virtual Numbers")

    bold = xlwt.easyxf("font: bold on")
    sheet.write(0, 0, "Порядковый номер", bold)
    sheet.write(0, 1, "Виртуальный номер", bold)

    for i, num in enumerate(numbers, start=1):
        sheet.write(i, 0, i)
        sheet.write(i, 1, num)

    sheet.col(0).width = 256 * 20
    sheet.col(1).width = 256 * 25

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer
