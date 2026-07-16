"""Super-admin settings panel (🔧 Настройки).

Reply-button entry:
  1. Admin presses '🔧 Настройки' → bot lists all approved registration
     chats with a small ✅/❌ flag next to each name.
  2. Admin taps a chat → detail screen with per-chat settings and a
     toggle button for auto-prodleniya (renewal via plain-text messages).

Only ADMINS from .env can open this panel; the reply button is already
hidden for non-admins by `get_admin_keyboard()`.
"""

import html

from aiogram import types

from data.config import ADMINS
from keyboards.default.admin import SETTINGS_BTN
from keyboards.inline.settings import (
    settings_action_cb,
    settings_chat_detail_keyboard,
    settings_close_cb,
    settings_group_cb,
    settings_groups_keyboard,
)
from loader import db, dp


def _is_admin(user_id: int) -> bool:
    return str(user_id) in ADMINS


def _detail_text(row: dict) -> str:
    """Build the per-chat settings detail message."""
    title = html.escape(row.get("title") or "—")
    chat_id = row["chat_id"]
    diller_name = row.get("diller_name") or f"id={row.get('diller_id')}"
    diller_name = html.escape(str(diller_name))
    prod_human = "✅ включено" if row.get("prodleniya_enabled") else "❌ отключено"
    return (
        f"📝 <b>{title}</b>\n"
        f"<b>Chat ID:</b> <code>{chat_id}</code>\n"
        f"<b>Diller:</b> {diller_name}\n\n"
        f"<b>Авто-продление:</b> {prod_human}"
    )


@dp.message_handler(
    lambda m: _is_admin(m.from_user.id),
    chat_type=types.ChatType.PRIVATE,
    text=SETTINGS_BTN,
    state="*",
)
async def open_settings(message: types.Message):
    chats = await db.list_registration_chats()
    if not chats:
        await message.answer(
            "🔧 <b>Настройки</b>\n\n"
            "Пока нет ни одной одобренной группы регистрации."
        )
        return
    chats = [dict(r) for r in chats]
    await message.answer(
        "🔧 <b>Настройки групп регистрации</b>\n\n"
        "Выберите группу, чтобы изменить её параметры:",
        reply_markup=settings_groups_keyboard(chats),
    )


@dp.callback_query_handler(
    settings_group_cb.filter(),
    lambda c: _is_admin(c.from_user.id),
    state="*",
)
async def show_chat_detail(call: types.CallbackQuery, callback_data: dict):
    chat_id = int(callback_data["chat_id"])
    chats = [dict(r) for r in await db.list_registration_chats()]
    row = next((r for r in chats if r["chat_id"] == chat_id), None)
    if not row:
        await call.answer("Группа не найдена.", show_alert=True)
        return
    try:
        await call.message.edit_text(
            _detail_text(row),
            reply_markup=settings_chat_detail_keyboard(
                chat_id, bool(row.get("prodleniya_enabled"))
            ),
        )
    except Exception:
        pass
    await call.answer()


@dp.callback_query_handler(
    settings_action_cb.filter(),
    lambda c: _is_admin(c.from_user.id),
    state="*",
)
async def apply_chat_action(call: types.CallbackQuery, callback_data: dict):
    chat_id = int(callback_data["chat_id"])
    action = callback_data["action"]

    if action == "back":
        chats = [dict(r) for r in await db.list_registration_chats()]
        if not chats:
            await call.message.edit_text(
                "🔧 <b>Настройки</b>\n\n"
                "Пока нет ни одной одобренной группы регистрации."
            )
            await call.answer()
            return
        try:
            await call.message.edit_text(
                "🔧 <b>Настройки групп регистрации</b>\n\n"
                "Выберите группу, чтобы изменить её параметры:",
                reply_markup=settings_groups_keyboard(chats),
            )
        except Exception:
            pass
        await call.answer()
        return

    if action in ("prod_on", "prod_off"):
        enabled = action == "prod_on"
        row = await db.set_chat_prodleniya(chat_id, enabled)
        if not row:
            await call.answer("Группа не найдена.", show_alert=True)
            return
        # Refetch with diller name for consistent detail rendering.
        chats = [dict(r) for r in await db.list_registration_chats()]
        row = next((r for r in chats if r["chat_id"] == chat_id), None) or dict(row)
        try:
            await call.message.edit_text(
                _detail_text(row),
                reply_markup=settings_chat_detail_keyboard(
                    chat_id, bool(row.get("prodleniya_enabled"))
                ),
            )
        except Exception:
            pass
        await call.answer("Готово.")
        return

    await call.answer("Неизвестное действие.", show_alert=True)


@dp.callback_query_handler(
    settings_close_cb.filter(),
    lambda c: _is_admin(c.from_user.id),
    state="*",
)
async def close_settings(call: types.CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()
