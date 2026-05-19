from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandStart
from aiogram.types import ReplyKeyboardRemove

from data.config import ADMINS
from keyboards.default.admin import get_admin_keyboard
from keyboards.default.phone import phone_keyboard
from loader import db, dp


def _post_auth_keyboard(user_id: int):
    return get_admin_keyboard() if str(user_id) in ADMINS else ReplyKeyboardRemove()


@dp.message_handler(CommandStart(), chat_type=types.ChatType.PRIVATE)
async def bot_start(message: types.Message):
    user = await db.select_user(user_id=message.from_user.id)
    if user:
        await message.answer(
            f"С возвращением, {user['full_name']}!",
            reply_markup=_post_auth_keyboard(message.from_user.id),
        )
        return

    await message.answer(
        f"Здравствуйте, {message.from_user.full_name}!\n"
        "Поделитесь, пожалуйста, номером телефона, чтобы продолжить.",
        reply_markup=phone_keyboard,
    )


@dp.message_handler(
    chat_type=types.ChatType.PRIVATE,
    content_types=types.ContentType.CONTACT,
)
async def get_contact(message: types.Message):
    contact = message.contact
    if contact.user_id != message.from_user.id:
        await message.answer("Пожалуйста, поделитесь своим контактом.")
        return

    if await db.select_user(user_id=message.from_user.id):
        await message.answer(
            "Вы уже зарегистрированы.",
            reply_markup=_post_auth_keyboard(message.from_user.id),
        )
        return

    await db.add_user(
        user_id=message.from_user.id,
        full_name=message.from_user.full_name,
        phone=contact.phone_number,
    )
    await message.answer(
        f"Спасибо, {message.from_user.full_name}! Вы успешно зарегистрированы.",
        reply_markup=_post_auth_keyboard(message.from_user.id),
    )
