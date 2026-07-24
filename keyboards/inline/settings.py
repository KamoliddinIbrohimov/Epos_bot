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

# Pick a specific diller from the fetched cazad-list to re-bind a chat.
settings_pick_diller_cb = CallbackData("settings_pick_dil", "chat_id", "diller_id")

# Top-level menu: pick one of two management sections.
#   section='root'    — top-level menu (used for «⬅️ Назад» from a section list)
#   section='groups'  — list of approved registration chats
#   section='dillers' — list of local dillers
settings_menu_cb = CallbackData("s_menu", "section")

# Diller management: view list, drill into one, ask/do delete.
#   action='view'     — show detail for `diller_id`
#   action='del_ask'  — confirmation prompt for `diller_id`
#   action='del_do'   — actually delete both cache row and mappings
settings_diller_mgmt_cb = CallbackData("s_dil_mgmt", "action", "diller_id")

# Detach a SPECIFIC (diller_id, chat_id) user-mapping without touching the
# diller itself. Two-step confirmation: 'ask' → 'do'.
settings_diller_chat_cb = CallbackData(
    "s_dil_ch", "action", "diller_id", "chat_id"
)


def settings_root_keyboard() -> InlineKeyboardMarkup:
    """Two-section root: pick either groups or dillers management."""
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(
            text="💬 Управление группами",
            callback_data=settings_menu_cb.new(section="groups"),
        ),
        InlineKeyboardButton(
            text="👥 Управление диллерами",
            callback_data=settings_menu_cb.new(section="dillers"),
        ),
        InlineKeyboardButton(
            text="❌ Закрыть", callback_data=settings_close_cb.new()
        ),
    )
    return kb


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
            text="⬅️ Назад в меню",
            callback_data=settings_menu_cb.new(section="root"),
        ),
        InlineKeyboardButton(
            text="❌ Закрыть", callback_data=settings_close_cb.new()
        ),
    )
    return kb


def settings_dillers_list_keyboard(dillers: Iterable[dict]) -> InlineKeyboardMarkup:
    """List of dillers cached locally. Each row: name + id + user-count.
    Click a row → detail screen with delete option."""
    kb = InlineKeyboardMarkup(row_width=1)
    for d in dillers:
        did = d.get("id")
        name = d.get("name") or "—"
        n_users = d.get("users_count") or 0
        n_chats = d.get("chats_count") or 0
        label = f"{name} (id={did}) · 👤{n_users} · 💬{n_chats}"[:60]
        kb.add(
            InlineKeyboardButton(
                text=label,
                callback_data=settings_diller_mgmt_cb.new(
                    action="view", diller_id=str(did)
                ),
            )
        )
    kb.add(
        InlineKeyboardButton(
            text="⬅️ Назад в меню",
            callback_data=settings_menu_cb.new(section="root"),
        ),
        InlineKeyboardButton(
            text="❌ Закрыть", callback_data=settings_close_cb.new()
        ),
    )
    return kb


def settings_diller_detail_keyboard(
    diller_id: int, user_chat_ids: Iterable[int] = ()
) -> InlineKeyboardMarkup:
    """One-diller detail. Renders:
      - a delete-button per attached user (removes just that mapping),
      - a full-nuke button (removes diller cache + all mappings),
      - back / close.
    """
    kb = InlineKeyboardMarkup(row_width=1)
    for cid in user_chat_ids:
        kb.add(
            InlineKeyboardButton(
                text=f"🗑 Отвязать юзера {cid}",
                callback_data=settings_diller_chat_cb.new(
                    action="del_ask",
                    diller_id=str(diller_id),
                    chat_id=str(cid),
                ),
            )
        )
    kb.add(
        InlineKeyboardButton(
            text="🗑 Удалить диллер + все привязки",
            callback_data=settings_diller_mgmt_cb.new(
                action="del_ask", diller_id=str(diller_id)
            ),
        ),
        InlineKeyboardButton(
            text="⬅️ К списку диллеров",
            callback_data=settings_menu_cb.new(section="dillers"),
        ),
        InlineKeyboardButton(
            text="❌ Закрыть", callback_data=settings_close_cb.new()
        ),
    )
    return kb


def settings_diller_chat_delete_confirm_keyboard(
    diller_id: int, chat_id: int
) -> InlineKeyboardMarkup:
    """Yes/No for detaching a single (diller_id, chat_id) pair."""
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(
            text="✅ Да, отвязать",
            callback_data=settings_diller_chat_cb.new(
                action="del_do",
                diller_id=str(diller_id),
                chat_id=str(chat_id),
            ),
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=settings_diller_mgmt_cb.new(
                action="view", diller_id=str(diller_id)
            ),
        ),
    )
    return kb


def settings_diller_delete_confirm_keyboard(diller_id: int) -> InlineKeyboardMarkup:
    """Yes/No confirmation for diller deletion."""
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(
            text="✅ Да, удалить",
            callback_data=settings_diller_mgmt_cb.new(
                action="del_do", diller_id=str(diller_id)
            ),
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=settings_diller_mgmt_cb.new(
                action="view", diller_id=str(diller_id)
            ),
        ),
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
            text="🔄 Сменить диллера",
            callback_data=settings_action_cb.new(
                chat_id=str(chat_id), action="change_diller"
            ),
        ),
        InlineKeyboardButton(
            text="⬅️ К списку групп",
            callback_data=settings_menu_cb.new(section="groups"),
        ),
        InlineKeyboardButton(
            text="❌ Закрыть", callback_data=settings_close_cb.new()
        ),
    )
    return kb


def settings_diller_list_keyboard(
    chat_id: int, dillers: Iterable[dict]
) -> InlineKeyboardMarkup:
    """List of cazad dillers to pick a new binding for a chat.
    Each button uses `settings_pick_diller_cb`; bottom row is back + close."""
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
                text=f"{name} (id={diller_id})"[:60],
                callback_data=settings_pick_diller_cb.new(
                    chat_id=str(chat_id), diller_id=str(diller_id)
                ),
            )
        )
    kb.add(
        InlineKeyboardButton(
            text="⬅️ Отмена",
            callback_data=settings_group_cb.new(chat_id=str(chat_id)),
        ),
        InlineKeyboardButton(
            text="❌ Закрыть", callback_data=settings_close_cb.new()
        ),
    )
    return kb
