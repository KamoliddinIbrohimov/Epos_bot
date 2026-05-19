from aiogram import types

from loader import dp


# Echo bot
@dp.message_handler(chat_type=types.ChatType.PRIVATE, state=None)
async def bot_echo(message: types.Message):
    await message.answer(message.text)
