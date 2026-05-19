from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.callback_data import CallbackData

chat_approval_cb = CallbackData("chat_approval", "action", "chat_id")
chat_diller_link_cb = CallbackData("chat_diller", "chat_id", "diller_id")


def chat_approval_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton(
            text="✅ Одобрить",
            callback_data=chat_approval_cb.new(action="approve", chat_id=chat_id),
        ),
        InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=chat_approval_cb.new(action="reject", chat_id=chat_id),
        ),
    )
    return keyboard


def chat_diller_link_keyboard(chat_id: int, dillers) -> InlineKeyboardMarkup:
    """Inline keyboard with diller names — used to link a freshly-approved group
    to a diller."""
    kb = InlineKeyboardMarkup(row_width=1)
    for d in dillers:
        if not isinstance(d, dict):
            continue
        diller_id = d.get("id") or d.get("pk")
        name = d.get("name") or "—"
        if diller_id is None:
            continue
        kb.add(
            InlineKeyboardButton(
                text=str(name),
                callback_data=chat_diller_link_cb.new(
                    chat_id=str(chat_id), diller_id=str(diller_id)
                ),
            )
        )
    return kb
