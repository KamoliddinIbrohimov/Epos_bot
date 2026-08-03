from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.callback_data import CallbackData

holiday_cb = CallbackData("holiday", "action", "date")


def holidays_keyboard(holidays: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    for d in sorted(holidays):
        kb.row(
            InlineKeyboardButton(
                f"📅 {d}",
                callback_data=holiday_cb.new(action="noop", date=str(d)),
            ),
            InlineKeyboardButton(
                "🗑 Удалить",
                callback_data=holiday_cb.new(action="del", date=str(d)),
            ),
        )
    kb.add(
        InlineKeyboardButton(
            "➕ Добавить",
            callback_data=holiday_cb.new(action="add", date="_"),
        )
    )
    return kb
