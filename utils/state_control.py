"""Generic mechanism for handling unexpected input inside any FSM state.

Each handler that enters a state is expected to call
`save_prompt(state, prompt_html)` (or to pass `_last_prompt` directly to
`state.update_data`). Then a fallback handler registered for that state
can call `prompt_continue_or_exit(message, state)` to ask the user
whether to abort or resend the prompt.

The two corresponding callback handlers live in
`handlers/users/state_control.py` and reset / re-show accordingly.
"""

from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.callback_data import CallbackData

state_control_cb = CallbackData("st_ctrl", "action")


def state_control_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(
            "🚪 Выйти", callback_data=state_control_cb.new(action="exit")
        ),
        InlineKeyboardButton(
            "🔄 Продолжить", callback_data=state_control_cb.new(action="continue")
        ),
    )
    return kb


async def save_prompt(state: FSMContext, prompt_html: str) -> None:
    """Запомнить текст промпта текущего шага, чтобы фолбэк/«продолжить»
    могли его повторно показать."""
    await state.update_data(_last_prompt=prompt_html)


async def prompt_continue_or_exit(
    message: types.Message,
    state: FSMContext,
    hint: str = "",
) -> None:
    """Показать пользователю кнопки «Выйти / Продолжить» с пояснением,
    что текущее сообщение не подходит к текущему шагу.

    `hint` — опциональная короткая подсказка, что именно не так
    (например, «нужно отправить .xlsx файл»).
    """
    text = "⚠️ Это сообщение не подходит для текущего шага."
    if hint:
        text += f"\n{hint}"
    await message.answer(text, reply_markup=state_control_keyboard())
