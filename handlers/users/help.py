from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandHelp

from loader import dp


@dp.message_handler(CommandHelp(), chat_type=types.ChatType.PRIVATE)
async def bot_help(message: types.Message):
    text = ("Commands: ",
            "/start - Start the bot",
            "/help - Help")
    
    await message.answer("\n".join(text))
