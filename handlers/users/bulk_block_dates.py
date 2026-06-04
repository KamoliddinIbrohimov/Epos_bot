"""Bulk block_date updates via xlsx upload.

Flow: dealer (or admin) sends a .xlsx file in a private chat. Bot
parses it (column A = fiscal number, column B = block date), validates
each row, and replies with a preview + confirm/cancel inline buttons.

  * Confirm  -> apply blocked_date update to every valid business that
                belongs to this dealer (or to anyone, if user is admin).
  * Cancel   -> finish FSM, no changes are made.

No reply-keyboard button — the trigger is the file itself.
"""

import html
import io
import logging
from datetime import date, datetime

import openpyxl
from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.callback_data import CallbackData

from data.config import ADMINS
from handlers.users.business import (
    UPDATABLE_FIELDS,
    _flatten_fk,
    _pick_business,
    update_business,
)
from loader import db, dp
from utils.diller import get_user_diller_name
from utils.epos_api import EposAPIError, epos_api
from utils.notify_groups import notify_log_groups


MAX_ROWS = 300


class BulkBlockDates(StatesGroup):
    confirming = State()


bbd_cb = CallbackData("bbd", "action")


def _bbd_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(
            "✅ Подтвердить", callback_data=bbd_cb.new(action="confirm")
        ),
        InlineKeyboardButton(
            "❌ Отмена", callback_data=bbd_cb.new(action="cancel")
        ),
    )
    return kb


def _normalize_date(value) -> str:
    """Return 'YYYY-MM-DD' or raise ValueError with a short reason."""
    if value is None:
        raise ValueError("пусто")
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value).strip()
    if not s:
        raise ValueError("пусто")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"непонятная дата: {s!r}")


def _is_admin(user_id: int) -> bool:
    return str(user_id) in ADMINS


@dp.message_handler(
    lambda m: bool(
        m.document
        and m.document.file_name
        and m.document.file_name.lower().endswith(".xlsx")
    ),
    chat_type=types.ChatType.PRIVATE,
    content_types=types.ContentType.DOCUMENT,
    state=None,
)
async def bbd_receive_xlsx(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    diller_ids = []
    is_admin = _is_admin(user_id)
    if not is_admin:
        diller_ids = await db.get_diller_ids_by_chat_id(user_id)
        if not diller_ids:
            await message.answer("⛔ Эта функция вам недоступна.")
            return

    buffer = io.BytesIO()
    try:
        await message.document.download(destination_file=buffer)
    except Exception as e:
        logging.exception("bbd download failed")
        await message.answer(f"⚠️ Не удалось скачать файл: {html.escape(str(e))}")
        return
    buffer.seek(0)

    try:
        wb = openpyxl.load_workbook(buffer, data_only=True, read_only=True)
        sheet = wb.active
        raw_rows = []
        for row in sheet.iter_rows(values_only=True):
            if not row:
                continue
            fn = row[0] if len(row) > 0 else None
            dt = row[1] if len(row) > 1 else None
            if fn is None and dt is None:
                continue
            raw_rows.append((fn, dt))
        wb.close()
    except Exception as e:
        logging.exception("bbd xlsx parse failed")
        await message.answer(
            f"⚠️ Не удалось прочитать .xlsx: {html.escape(str(e))}"
        )
        return

    if not raw_rows:
        await message.answer("⚠️ Файл пустой.")
        return

    if len(raw_rows) > MAX_ROWS:
        await message.answer(
            f"⚠️ В файле <b>{len(raw_rows)}</b> строк — это больше "
            f"допустимого лимита <b>{MAX_ROWS}</b>.\n"
            f"Разбей на несколько файлов по {MAX_ROWS} строк и пришли заново."
        )
        return

    valid = []
    invalid = []
    for idx, (fn_cell, dt_cell) in enumerate(raw_rows, start=1):
        fn = str(fn_cell).strip() if fn_cell is not None else ""
        if not fn:
            invalid.append((idx, fn_cell, dt_cell, "пустой фискальный"))
            continue
        try:
            iso = _normalize_date(dt_cell)
        except ValueError as e:
            invalid.append((idx, fn, dt_cell, str(e)))
            continue
        valid.append((fn, iso))

    if not valid:
        invalid_sample = "\n".join(
            f"• строка {idx}: <code>{html.escape(str(fn))}</code> "
            f"+ <code>{html.escape(str(dt))}</code> — {html.escape(reason)}"
            for idx, fn, dt, reason in invalid[:10]
        )
        await message.answer(
            "⚠️ <b>Файл не содержит корректных строк</b>\n\n"
            f"Проверено: {len(raw_rows)}, все с ошибками.\n\n"
            f"Примеры:\n{invalid_sample}\n\n"
            "Ожидаемый формат: колонка A — фискальный номер (name), "
            "колонка B — дата вида <code>2026-08-01</code>."
        )
        return

    await state.update_data(
        bbd_rows=valid,
        bbd_is_admin=is_admin,
        bbd_diller_ids=diller_ids,
    )
    await BulkBlockDates.confirming.set()

    preview_lines = [
        f"• <code>{html.escape(fn)}</code> → <code>{html.escape(iso)}</code>"
        for fn, iso in valid[:10]
    ]
    extra = f"\n…ещё {len(valid) - 10}" if len(valid) > 10 else ""
    invalid_line = (
        f"\n<b>С ошибками формата:</b> {len(invalid)}" if invalid else ""
    )

    text = (
        f"📋 <b>Файл разобран</b>\n"
        f"Корректных строк: <b>{len(valid)}</b>"
        f"{invalid_line}\n\n"
        f"<b>Предпросмотр (до 10):</b>\n"
        + "\n".join(preview_lines)
        + extra
        + "\n\nПрименить эти даты блокировки?"
    )
    await message.answer(text, reply_markup=_bbd_keyboard())


@dp.callback_query_handler(
    bbd_cb.filter(action="cancel"),
    state=BulkBlockDates.confirming,
)
async def bbd_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.finish()
    try:
        await callback.message.edit_text(
            "❌ Отменено. Изменения не применены."
        )
    except Exception:
        pass
    await callback.answer("Отменено")


@dp.callback_query_handler(
    bbd_cb.filter(action="confirm"),
    state=BulkBlockDates.confirming,
)
async def bbd_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    rows = data.get("bbd_rows") or []
    is_admin = bool(data.get("bbd_is_admin"))
    diller_ids = data.get("bbd_diller_ids") or []

    if not rows:
        await callback.message.edit_text(
            "⚠️ Состояние утеряно, отправь файл заново."
        )
        await state.finish()
        await callback.answer()
        return

    try:
        await callback.message.edit_text(
            f"⏳ Применяю {len(rows)} изменений…"
        )
    except Exception:
        pass

    try:
        token = await epos_api.get_token()
    except EposAPIError as e:
        await callback.message.edit_text(f"⚠️ get_token: {html.escape(str(e))}")
        await state.finish()
        await callback.answer()
        return

    # Лениво — не тянем во время import (порядок регистрации хендлеров).
    from handlers.users.find_business import _branch_text, _summary, get_business_by_name

    user = callback.from_user
    full_name = html.escape(user.full_name)
    diller_name_sender = await get_user_diller_name(user.id) or "—"

    updated = []
    skipped_other = []
    not_found = []
    errors = []

    for fn, blocked_iso in rows:
        try:
            response = await get_business_by_name(fn, token)
        except EposAPIError as e:
            errors.append((fn, str(e)))
            continue

        business = _pick_business(response)
        business_id = business.get("id") or business.get("business_id")
        if not business_id:
            not_found.append(fn)
            continue

        current_diller = _flatten_fk(business.get("diller"))
        if not is_admin and current_diller not in diller_ids:
            skipped_other.append(fn)
            continue

        payload = {
            key: _flatten_fk(business.get(key))
            for key in UPDATABLE_FIELDS
            if key in business
        }
        payload["blocked_date"] = blocked_iso

        try:
            await update_business(business_id, token, **payload)
        except EposAPIError as e:
            errors.append((fn, str(e)))
            continue

        updated.append((fn, blocked_iso))

        # Notify per-diller log chat + central PDF_GROUP_CHAT_ID
        tin = business.get("TIN") or business.get("tin") or "—"
        branch = _branch_text(business)
        group_text = (
            f"🔒 <b>Обновлена дата блокировки</b> (bulk)\n"
            f"<b>Diller:</b> {html.escape(str(diller_name_sender))}\n"
            f'От: <a href="tg://user?id={user.id}">{full_name}</a> '
            f"(id: <code>{user.id}</code>)\n\n"
            + _summary(fn, tin, branch, blocked_iso)
        )
        try:
            await notify_log_groups(current_diller, group_text)
        except Exception as exc:
            logging.exception(
                f"bbd notify_log_groups failed for {fn}: {exc}"
            )

    lines = [
        "✅ Готово.",
        f"Обновлено: <b>{len(updated)}</b>",
    ]
    if skipped_other:
        lines.append(f"Не ваш дилер: {len(skipped_other)}")
    if not_found:
        lines.append(f"Не найдено в Cazad: {len(not_found)}")
    if errors:
        lines.append(f"Ошибки API: {len(errors)}")

    detail = []
    if skipped_other:
        sample = ", ".join(
            f"<code>{html.escape(x)}</code>" for x in skipped_other[:5]
        )
        detail.append(f"\n<b>Чужие:</b> {sample}")
    if not_found:
        sample = ", ".join(
            f"<code>{html.escape(x)}</code>" for x in not_found[:5]
        )
        detail.append(f"\n<b>Не найдены:</b> {sample}")
    if errors:
        sample = ", ".join(
            f"<code>{html.escape(fn)}</code>" for fn, _ in errors[:5]
        )
        detail.append(f"\n<b>Ошибки:</b> {sample}")

    final = "\n".join(lines) + "".join(detail)
    try:
        await callback.message.edit_text(final)
    except Exception:
        await callback.message.answer(final)

    await state.finish()
    await callback.answer()
