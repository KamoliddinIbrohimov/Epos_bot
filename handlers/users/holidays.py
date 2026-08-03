"""Admin panel for managing public holidays.

Admins can add/remove dates that block dates should never fall on.
Supports single dates and date ranges (e.g. "2026-04-30 2026-05-02").
Multiple lines accepted in one message.
"""

import re
from datetime import date, timedelta

from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

from data.config import ADMINS
from keyboards.default.admin import HOLIDAYS_BTN
from keyboards.inline.holidays import holiday_cb, holidays_keyboard
from loader import db, dp

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class HolidaysStates(StatesGroup):
    waiting_for_dates = State()


def _is_admin(user_id: int) -> bool:
    return str(user_id) in ADMINS


def _parse_holiday_input(text: str):
    """Parse admin input into (valid_dates, error_strings).

    Each line can be:
      - single date: "2026-03-08"
      - range (two dates): "2026-04-30 2026-05-02"  → expands to all days inclusive
    """
    valid = []
    errors = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        found = _DATE_RE.findall(line)
        if not found:
            errors.append(f"<code>{line}</code> — sana topilmadi")
            continue
        if len(found) == 1:
            try:
                valid.append(date.fromisoformat(found[0]))
            except ValueError:
                errors.append(f"<code>{found[0]}</code> — noto'g'ri sana")
        elif len(found) == 2:
            try:
                d1 = date.fromisoformat(found[0])
                d2 = date.fromisoformat(found[1])
                if d1 > d2:
                    d1, d2 = d2, d1
                cur = d1
                while cur <= d2:
                    valid.append(cur)
                    cur += timedelta(days=1)
            except ValueError:
                errors.append(f"<code>{line}</code> — noto'g'ri diapazon")
        else:
            errors.append(f"<code>{line}</code> — satrda 2 tadan ko'p sana")
    return valid, errors


async def _show_holidays_list(target, edit: bool = False):
    holidays = await db.list_holidays()
    if holidays:
        lines = "\n".join(f"• <code>{d}</code>" for d in holidays)
        text = f"📅 <b>Праздничные дни</b>\n\n{lines}"
    else:
        text = "📅 <b>Праздничные дни</b>\n\nСписок пуст."
    kb = holidays_keyboard(holidays)
    if edit:
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except Exception:
            await target.message.answer(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@dp.message_handler(
    lambda m: m.text == HOLIDAYS_BTN,
    chat_type=types.ChatType.PRIVATE,
    state="*",
)
async def show_holidays_menu(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.finish()
    await _show_holidays_list(message)


@dp.callback_query_handler(
    holiday_cb.filter(action="add"),
    state="*",
)
async def holidays_add_prompt(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await HolidaysStates.waiting_for_dates.set()
    await callback.message.edit_text(
        "📅 <b>Отправьте дату праздника</b>\n\n"
        "Форматы:\n"
        "• Одна дата: <code>2026-03-08</code>\n"
        "• Диапазон: <code>2026-04-30 2026-05-02</code>\n"
        "• Несколько строк — по одной дате или диапазону на строку\n\n"
        "Отмена: /cancel"
    )
    await callback.answer()


@dp.message_handler(
    commands=["cancel"],
    state=HolidaysStates.waiting_for_dates,
    chat_type=types.ChatType.PRIVATE,
)
async def holidays_add_cancel(message: types.Message, state: FSMContext):
    await state.finish()
    await _show_holidays_list(message)


@dp.message_handler(
    state=HolidaysStates.waiting_for_dates,
    content_types=types.ContentType.TEXT,
    chat_type=types.ChatType.PRIVATE,
)
async def holidays_receive_dates(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        await state.finish()
        return

    valid, errors = _parse_holiday_input(message.text)
    if not valid:
        err_text = "\n".join(errors[:5])
        await message.answer(
            f"⚠️ Не найдено ни одной корректной даты.\n\n{err_text}\n\n"
            "Попробуйте снова или /cancel"
        )
        return

    for d in valid:
        await db.add_holiday(d)

    await state.finish()

    added = sorted(valid)
    preview = ", ".join(f"<code>{d}</code>" for d in added[:10])
    extra = f" и ещё {len(added) - 10}" if len(added) > 10 else ""
    note = f"\n⚠️ Ошибок: {len(errors)}" if errors else ""
    await message.answer(f"✅ Добавлено: {preview}{extra}{note}")
    await _show_holidays_list(message)


@dp.callback_query_handler(
    holiday_cb.filter(action="noop"),
    state="*",
)
async def holidays_noop(callback: types.CallbackQuery, **_):
    await callback.answer()


@dp.callback_query_handler(
    holiday_cb.filter(action="del"),
    state="*",
)
async def holidays_delete(
    callback: types.CallbackQuery,
    callback_data: dict,
    state: FSMContext,
):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    d = date.fromisoformat(callback_data["date"])
    await db.remove_holiday(d)
    await callback.answer(f"✅ {d} удалён")
    await _show_holidays_list(callback, edit=True)
