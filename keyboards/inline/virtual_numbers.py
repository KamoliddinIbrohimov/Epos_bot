from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.callback_data import CallbackData

virtual_numbers_cb = CallbackData("virt_num", "action")


def virtual_numbers_confirm_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton(
            text="✅ Подтвердить",
            callback_data=virtual_numbers_cb.new(action="confirm"),
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=virtual_numbers_cb.new(action="cancel"),
        ),
    )
    return keyboard
