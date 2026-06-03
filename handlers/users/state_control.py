import html

from aiogram import types
from aiogram.dispatcher import FSMContext

from loader import dp
from utils.state_control import state_control_cb


@dp.callback_query_handler(state_control_cb.filter(action="exit"), state="*")
async def state_exit(callback: types.CallbackQuery, state: FSMContext):
    await state.finish()
    try:
        await callback.message.edit_text("❌ Действие отменено.")
    except Exception:
        try:
            await callback.message.answer("❌ Действие отменено.")
        except Exception:
            pass
    await callback.answer("Отменено")


@dp.callback_query_handler(state_control_cb.filter(action="continue"), state="*")
async def state_continue(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    prompt = data.get("_last_prompt") or "Продолжайте."
    try:
        await callback.message.edit_text(prompt)
    except Exception:
        try:
            await callback.message.answer(prompt)
        except Exception:
            pass
    await callback.answer()
