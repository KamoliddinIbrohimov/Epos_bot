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
    settings_diller_chat_cb,
    settings_diller_chat_delete_confirm_keyboard,
    settings_diller_delete_confirm_keyboard,
    settings_diller_detail_keyboard,
    settings_diller_list_keyboard,
    settings_diller_mgmt_cb,
    settings_dillers_list_keyboard,
    settings_group_cb,
    settings_groups_keyboard,
    settings_menu_cb,
    settings_pick_diller_cb,
    settings_root_keyboard,
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


_ROOT_MENU_TEXT = (
    "🔧 <b>Настройки</b>\n\n"
    "Что настраиваем?\n"
    "  • <b>Группы</b> — авто-продление, привязанный диллер\n"
    "  • <b>Диллеры</b> — локальный кэш и user-привязки"
)
_GROUPS_MENU_TEXT = (
    "💬 <b>Управление группами</b>\n\n"
    "Выберите группу, чтобы изменить её параметры:"
)
_DILLERS_MENU_TEXT = (
    "👥 <b>Управление диллерами</b>\n\n"
    "Формат строки: <i>Имя (id) · 👤 привязанных юзеров · "
    "💬 привязанных чатов</i>.\n\n"
    "Клик по диллеру — детали и удаление."
)


async def _render_groups(target) -> None:
    """target is Message or CallbackQuery.message — both support edit_text/answer."""
    chats = [dict(r) for r in await db.list_registration_chats()]
    if not chats:
        text = (
            "💬 <b>Управление группами</b>\n\n"
            "Пока нет ни одной одобренной группы регистрации."
        )
        try:
            await target.edit_text(text, reply_markup=settings_root_keyboard())
        except AttributeError:
            await target.answer(text, reply_markup=settings_root_keyboard())
        return
    try:
        await target.edit_text(
            _GROUPS_MENU_TEXT, reply_markup=settings_groups_keyboard(chats)
        )
    except AttributeError:
        await target.answer(
            _GROUPS_MENU_TEXT, reply_markup=settings_groups_keyboard(chats)
        )


async def _render_dillers(target) -> None:
    dillers = [dict(r) for r in await db.list_local_dillers_with_counts()]
    if not dillers:
        text = (
            "👥 <b>Управление диллерами</b>\n\n"
            "Локальный кэш пуст. Добавь диллеров через кнопку "
            "«Добавить дилера» на главной клавиатуре."
        )
        try:
            await target.edit_text(text, reply_markup=settings_root_keyboard())
        except AttributeError:
            await target.answer(text, reply_markup=settings_root_keyboard())
        return
    try:
        await target.edit_text(
            _DILLERS_MENU_TEXT,
            reply_markup=settings_dillers_list_keyboard(dillers),
        )
    except AttributeError:
        await target.answer(
            _DILLERS_MENU_TEXT,
            reply_markup=settings_dillers_list_keyboard(dillers),
        )


@dp.message_handler(
    lambda m: _is_admin(m.from_user.id),
    chat_type=types.ChatType.PRIVATE,
    text=SETTINGS_BTN,
    state="*",
)
async def open_settings(message: types.Message):
    await message.answer(_ROOT_MENU_TEXT, reply_markup=settings_root_keyboard())


@dp.callback_query_handler(
    settings_menu_cb.filter(),
    lambda c: _is_admin(c.from_user.id),
    state="*",
)
async def switch_section(call: types.CallbackQuery, callback_data: dict):
    section = callback_data.get("section")
    if section == "root":
        try:
            await call.message.edit_text(
                _ROOT_MENU_TEXT, reply_markup=settings_root_keyboard()
            )
        except Exception:
            pass
        await call.answer()
        return
    if section == "groups":
        await _render_groups(call.message)
        await call.answer()
        return
    if section == "dillers":
        await _render_dillers(call.message)
        await call.answer()
        return
    await call.answer("Неизвестный раздел.", show_alert=True)


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
        # Совместимость со старыми клавиатурами: возвращаем к списку групп.
        await _render_groups(call.message)
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

    if action == "change_diller":
        # Fetch fresh cazad dillers; show as inline list to re-bind.
        from handlers.users.dillers import _pick_list, get_dillers
        from utils.epos_api import EposAPIError, epos_api
        try:
            token = await epos_api.get_token()
            data = await get_dillers(token)
        except EposAPIError as e:
            await call.answer(f"E-POS: {e}"[:180], show_alert=True)
            return
        dillers = _pick_list(data)
        if not dillers:
            await call.answer("Список диллеров пуст.", show_alert=True)
            return
        try:
            await call.message.edit_text(
                "🔄 <b>Смена диллера</b>\n\n"
                "Выберите нового диллера для этой группы. "
                "Локальная привязка обновится сразу, cazad не трогаем.",
                reply_markup=settings_diller_list_keyboard(chat_id, dillers),
            )
        except Exception:
            pass
        await call.answer()
        return

    await call.answer("Неизвестное действие.", show_alert=True)


@dp.callback_query_handler(
    settings_diller_mgmt_cb.filter(),
    lambda c: _is_admin(c.from_user.id),
    state="*",
)
async def diller_management(call: types.CallbackQuery, callback_data: dict):
    action = callback_data["action"]
    diller_id_raw = callback_data.get("diller_id") or "0"

    if action == "list":
        # Совместимость со старыми клавиатурами.
        await _render_dillers(call.message)
        await call.answer()
        return

    try:
        diller_id = int(diller_id_raw)
    except (TypeError, ValueError):
        await call.answer("Некорректный id.", show_alert=True)
        return

    if action == "view":
        dillers = [dict(r) for r in await db.list_local_dillers_with_counts()]
        d = next((x for x in dillers if x["id"] == diller_id), None)
        if not d:
            await call.answer("Диллер не найден.", show_alert=True)
            return
        user_ids = await db.list_diller_user_mappings(diller_id)
        users_line = (
            "\n".join(f"  • <code>{cid}</code>" for cid in user_ids)
            if user_ids
            else "  —"
        )
        text = (
            f"👤 <b>{html.escape(d.get('name') or '—')}</b>\n"
            f"<b>ID:</b> <code>{d['id']}</code>\n"
            f"<b>Привязано юзеров (diller_chats):</b> {d.get('users_count') or 0}\n"
            f"<b>Привязано чатов (chats):</b> {d.get('chats_count') or 0}\n\n"
            f"<b>Список юзеров:</b>\n{users_line}\n\n"
            "🗑 <b>Отвязать юзера</b> — удаляет ОДНУ строку из "
            "<code>diller_chats</code>, диллер остаётся.\n"
            "🗑 <b>Удалить диллер</b> — сносит и кэш, и ВСЕ привязки. "
            "Cazad не трогается в обоих случаях."
        )
        try:
            await call.message.edit_text(
                text,
                reply_markup=settings_diller_detail_keyboard(
                    diller_id, user_ids
                ),
            )
        except Exception:
            pass
        await call.answer()
        return

    if action == "del_ask":
        dillers = [dict(r) for r in await db.list_local_dillers_with_counts()]
        d = next((x for x in dillers if x["id"] == diller_id), None)
        if not d:
            await call.answer("Диллер не найден.", show_alert=True)
            return
        n_users = d.get("users_count") or 0
        n_chats = d.get("chats_count") or 0
        text = (
            f"⚠️ Удалить диллера <b>{html.escape(d.get('name') or '—')}</b> "
            f"(id=<code>{diller_id}</code>)?\n\n"
            f"Также будет удалено <b>{n_users}</b> user-привязок.\n"
            f"Чаты, привязанные к этому диллеру ({n_chats}), НЕ переустанавливаются "
            "автоматически — их нужно перепривязать через «🔄 Сменить диллера» "
            "в настройках группы."
        )
        try:
            await call.message.edit_text(
                text,
                reply_markup=settings_diller_delete_confirm_keyboard(diller_id),
            )
        except Exception:
            pass
        await call.answer()
        return

    if action == "del_do":
        await db.delete_diller_completely(diller_id)
        # Возвращаемся к обновлённому списку.
        await _render_dillers(call.message)
        await call.answer("Удалено.")
        return


@dp.callback_query_handler(
    settings_diller_chat_cb.filter(),
    lambda c: _is_admin(c.from_user.id),
    state="*",
)
async def diller_user_mapping(call: types.CallbackQuery, callback_data: dict):
    """Detach a single (diller_id, chat_id) pair — keeps the diller alive."""
    action = callback_data["action"]
    try:
        diller_id = int(callback_data["diller_id"])
        chat_id = int(callback_data["chat_id"])
    except (KeyError, TypeError, ValueError):
        await call.answer("Некорректные данные.", show_alert=True)
        return

    if action == "del_ask":
        text = (
            f"⚠️ Отвязать юзера <code>{chat_id}</code> от диллера "
            f"id=<code>{diller_id}</code>?\n\n"
            "Диллер и его остальные привязки останутся на месте."
        )
        try:
            await call.message.edit_text(
                text,
                reply_markup=settings_diller_chat_delete_confirm_keyboard(
                    diller_id, chat_id
                ),
            )
        except Exception:
            pass
        await call.answer()
        return

    if action == "del_do":
        await db.delete_diller_user_mapping(diller_id, chat_id)
        # Возвращаемся на экран деталей диллера с обновлённым списком.
        # Переиспользуем логику из diller_management(action='view').
        await diller_management(
            call,
            {"action": "view", "diller_id": str(diller_id)},
        )
        # diller_management уже дёрнул call.answer(), но повторный no-op тут
        # безопасен, дадим короткое подтверждение.
        try:
            await call.answer("Отвязано.")
        except Exception:
            pass
        return

    await call.answer("Неизвестное действие.", show_alert=True)

    await call.answer("Неизвестное действие.", show_alert=True)


@dp.callback_query_handler(
    settings_pick_diller_cb.filter(),
    lambda c: _is_admin(c.from_user.id),
    state="*",
)
async def pick_new_diller(call: types.CallbackQuery, callback_data: dict):
    chat_id = int(callback_data["chat_id"])
    diller_id = int(callback_data["diller_id"])

    row = await db.set_chat_diller(chat_id, diller_id)
    if not row:
        await call.answer("Группа не найдена.", show_alert=True)
        return

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
    await call.answer("Диллер обновлён.")


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
