"""Inline keyboards for the super-admin '🔧 Настройки' panel.

Two-level UI:
  1. Group list — pick which registration chat to configure.
  2. Per-chat detail — toggle `prodleniya_enabled` on/off, or go back.
"""

from typing import Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.callback_data import CallbackData


# Chat selected from the settings list.
settings_group_cb = CallbackData("settings_group", "chat_id")

# Action inside per-chat settings screen: toggle prodleniya, or go back.
settings_action_cb = CallbackData("settings_action", "chat_id", "action")

# Close the whole settings menu (delete message).
settings_close_cb = CallbackData("settings_close")


def settings_groups_keyboard(chats: Iterable[dict]) -> InlineKeyboardMarkup:
    """List of approved registration chats. Each row is one chat; the
    label shows current prodleniya flag so admin can see state at a glance."""
    kb = InlineKeyboardMarkup(row_width=1)
    for row in chats:
        flag = "✅" if row.get("prodleniya_enabled") else "❌"
        title = row.get("title") or "—"
        label = f"{flag} {title}"[:60]
        kb.add(
            InlineKeyboardButton(
                text=label,
                callback_data=settings_group_cb.new(chat_id=str(row["chat_id"])),
            )
        )
    kb.add(
        InlineKeyboardButton(
            text="❌ Закрыть", callback_data=settings_close_cb.new()
        )
    )
    return kb


def settings_chat_detail_keyboard(
    chat_id: int, prodleniya_enabled: bool
) -> InlineKeyboardMarkup:
    """Per-chat settings: single toggle for prodleniya + back button."""
    kb = InlineKeyboardMarkup(row_width=1)
    toggle_label = (
        "❌ Отключить авто-продление"
        if prodleniya_enabled
        else "✅ Включить авто-продление"
    )
    toggle_action = "prod_off" if prodleniya_enabled else "prod_on"
    kb.add(
        InlineKeyboardButton(
            text=toggle_label,
            callback_data=settings_action_cb.new(
                chat_id=str(chat_id), action=toggle_action
            ),
        ),
        InlineKeyboardButton(
            text="⬅️ К списку групп",
            callback_data=settings_action_cb.new(
                chat_id=str(chat_id), action="back"
            ),
        ),
        InlineKeyboardButton(
            text="❌ Закрыть", callback_data=settings_close_cb.new()
        ),
    )
    return kb
