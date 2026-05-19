from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.callback_data import CallbackData

diller_cb = CallbackData("diller", "diller_id")
attach_diller_cb = CallbackData("attach_diller", "diller_id")


def dillers_keyboard(dillers) -> InlineKeyboardMarkup:
    """Build an inline keyboard of diller name buttons (for AddDiller flow).

    `dillers` is a list of dicts with at least `id` and `name`.
    """
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
                callback_data=diller_cb.new(diller_id=str(diller_id)),
            )
        )
    return kb


def attach_dillers_keyboard(dillers) -> InlineKeyboardMarkup:
    """Inline keyboard of diller name buttons for AttachBusiness flow."""
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
                callback_data=attach_diller_cb.new(diller_id=str(diller_id)),
            )
        )
    return kb
