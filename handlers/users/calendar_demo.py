from aiogram import types
from aiogram_calendar import SimpleCalendar, simple_cal_callback

from loader import dp


@dp.message_handler(commands="calendar")
async def show_calendar(message: types.Message):
    await message.answer(
        "Выберите дату:",
        reply_markup=await SimpleCalendar().start_calendar(),
    )


@dp.callback_query_handler(simple_cal_callback.filter(), state=None)
async def process_calendar(callback: types.CallbackQuery, callback_data: dict):
    selected, picked_date = await SimpleCalendar().process_selection(
        callback, callback_data
    )
    if selected:
        await callback.message.answer(
            f"Вы выбрали: <code>{picked_date.strftime('%Y-%m-%d')}</code>"
        )
