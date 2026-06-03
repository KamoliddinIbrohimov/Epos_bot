import html
import logging

from aiogram import types

from data.config import ADMINS
from handlers.users.dillers import _pick_list, get_dillers
from keyboards.inline.chat_approval import (
    chat_approval_cb,
    chat_approval_keyboard,
    chat_diller_link_cb,
    chat_diller_link_keyboard,
    chat_group_type_cb,
    chat_group_type_keyboard,
)
from loader import bot, db, dp
from utils.epos_api import EposAPIError, epos_api

_PRESENT = {
    types.ChatMemberStatus.MEMBER,
    types.ChatMemberStatus.ADMINISTRATOR,
    types.ChatMemberStatus.CREATOR,
}
_ABSENT = {
    types.ChatMemberStatus.LEFT,
    types.ChatMemberStatus.KICKED,
}


@dp.my_chat_member_handler()
async def on_bot_added_to_group(update: types.ChatMemberUpdated):
    chat = update.chat
    if chat.type not in (types.ChatType.GROUP, types.ChatType.SUPERGROUP):
        return

    if update.old_chat_member.status not in _ABSENT:
        return
    if update.new_chat_member.status not in _PRESENT:
        return

    added_by = update.from_user
    added_by_name = html.escape(added_by.full_name)
    chat_title = html.escape(chat.title or "—")

    await db.upsert_pending_chat(
        chat_id=chat.id,
        title=chat.title or "",
        added_by=added_by.id,
    )

    text = (
        "🤖 Бот добавлен в группу — требуется одобрение.\n\n"
        f"📛 Название: <b>{chat_title}</b>\n"
        f"🆔 Chat ID: <code>{chat.id}</code>\n"
        f'➕ Добавил: <a href="tg://user?id={added_by.id}">{added_by_name}</a> '
        f"(id: <code>{added_by.id}</code>)"
    )
    markup = chat_approval_keyboard(chat.id)

    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
        except Exception as e:
            logging.exception(f"failed to notify admin {admin_id}: {e}")

    try:
        await bot.send_message(
            chat.id,
            "⏳ Ожидаю одобрения администратора. После одобрения "
            "я смогу принимать PDF-файлы в этой группе.",
        )
    except Exception as e:
        logging.exception(f"failed to notify group {chat.id}: {e}")


@dp.callback_query_handler(
    chat_approval_cb.filter(),
    lambda c: str(c.from_user.id) in ADMINS,
)
async def on_chat_approval(call: types.CallbackQuery, callback_data: dict):
    action = callback_data["action"]
    chat_id = int(callback_data["chat_id"])

    if action == "reject":
        row = await db.set_chat_status(chat_id, "rejected")
        if not row:
            await call.answer("Группа не найдена в базе.", show_alert=True)
            return
        admin_name = html.escape(call.from_user.full_name)
        new_text = (
            f"{call.message.html_text}\n\n"
            f"<b>Статус:</b> ❌ отклонена ({admin_name})"
        )
        try:
            await call.message.edit_text(new_text, reply_markup=None)
        except Exception as e:
            logging.exception(f"failed to edit admin message: {e}")
        try:
            await bot.send_message(
                chat_id,
                "❌ Группа отклонена администратором. Бот не будет обрабатывать сообщения.",
            )
        except Exception as e:
            logging.exception(f"failed to notify group {chat_id}: {e}")
        try:
            await bot.leave_chat(chat_id)
        except Exception as e:
            logging.exception(f"failed to leave chat {chat_id}: {e}")
        await call.answer("Готово.")
        return

    # Approval: status пока остаётся pending — статус выставится в "approved"
    # только когда админ выберет дилера для привязки.
    try:
        token = await epos_api.get_token()
        dillers_payload = await get_dillers(token)
    except EposAPIError as e:
        logging.exception("get_dillers failed during chat approval")
        await call.answer(f"E-POS error: {e}", show_alert=True)
        return

    dillers = _pick_list(dillers_payload)
    if not dillers:
        await call.answer("Список дилеров пуст.", show_alert=True)
        return

    admin_name = html.escape(call.from_user.full_name)
    new_text = (
        f"{call.message.html_text}\n\n"
        f"<b>Статус:</b> ⏳ выберите дилера для привязки ({admin_name})"
    )
    try:
        await call.message.edit_text(
            new_text,
            reply_markup=chat_diller_link_keyboard(chat_id, dillers),
        )
    except Exception as e:
        logging.exception(f"failed to edit admin message: {e}")
    await call.answer()


@dp.callback_query_handler(
    chat_diller_link_cb.filter(),
    lambda c: str(c.from_user.id) in ADMINS,
)
async def on_chat_diller_link(call: types.CallbackQuery, callback_data: dict):
    chat_id = int(callback_data["chat_id"])
    diller_id = int(callback_data["diller_id"])

    row = await db.set_chat_diller(chat_id, diller_id)
    if not row:
        await call.answer("Группа не найдена в базе.", show_alert=True)
        return

    diller_row = await db.get_diller(diller_id)
    diller_name = (
        diller_row["name"] if diller_row and diller_row.get("name")
        else f"id={diller_id}"
    )
    admin_name = html.escape(call.from_user.full_name)

    new_text = (
        f"{call.message.html_text.split(chr(10) + chr(10) + '<b>Статус:</b>')[0]}\n\n"
        f"<b>Дилер:</b> <b>{html.escape(diller_name)}</b>\n"
        f"<b>Статус:</b> ⏳ выберите тип группы ({admin_name})"
    )
    try:
        await call.message.edit_text(
            new_text,
            reply_markup=chat_group_type_keyboard(chat_id),
        )
    except Exception as e:
        logging.exception(f"failed to edit admin message: {e}")

    await call.answer()


@dp.callback_query_handler(
    chat_group_type_cb.filter(),
    lambda c: str(c.from_user.id) in ADMINS,
)
async def on_chat_group_type(call: types.CallbackQuery, callback_data: dict):
    chat_id = int(callback_data["chat_id"])
    gtype = callback_data["gtype"]
    if gtype not in ("registration", "log"):
        await call.answer("Неизвестный тип.", show_alert=True)
        return

    row = await db.set_chat_group_type(chat_id, gtype)
    if not row:
        await call.answer("Группа не найдена в базе.", show_alert=True)
        return

    diller_id = row.get("diller_id")
    diller_row = await db.get_diller(diller_id) if diller_id else None
    diller_name = (
        diller_row["name"] if diller_row and diller_row.get("name")
        else f"id={diller_id}"
    )
    admin_name = html.escape(call.from_user.full_name)
    gtype_human = (
        "📝 Регистрация" if gtype == "registration" else "📊 Лог"
    )

    new_text = (
        f"{call.message.html_text.split(chr(10) + chr(10) + '<b>Дилер:</b>')[0]}\n\n"
        f"<b>Дилер:</b> <b>{html.escape(diller_name)}</b>\n"
        f"<b>Тип:</b> {gtype_human}\n"
        f"<b>Статус:</b> ✅ одобрена ({admin_name})"
    )
    try:
        await call.message.edit_text(new_text, reply_markup=None)
    except Exception as e:
        logging.exception(f"failed to edit admin message: {e}")

    if gtype == "registration":
        chat_announce = (
            f"✅ Группа одобрена и привязана к дилеру "
            f"<b>{html.escape(diller_name)}</b>.\n"
            f"Тип: <b>📝 Регистрация</b> — можете отправлять PDF-файлы."
        )
    else:
        chat_announce = (
            f"✅ Группа одобрена и привязана к дилеру "
            f"<b>{html.escape(diller_name)}</b>.\n"
            f"Тип: <b>📊 Лог</b> — сюда будут приходить уведомления о "
            f"событиях по клиентам этого дилера."
        )
    try:
        await bot.send_message(chat_id, chat_announce)
    except Exception as e:
        logging.exception(f"failed to notify group {chat_id}: {e}")

    await call.answer("Готово.")
