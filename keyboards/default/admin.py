from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

ADD_VIRTUAL_NUMBERS_BTN = "Добавить виртуальные номера"
ADD_DILLER_BTN = "Добавить дилера"
ATTACH_BUSINESS_BTN = "Привязать business к дилеру"


def get_admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ADD_VIRTUAL_NUMBERS_BTN)],
            [KeyboardButton(text=ADD_DILLER_BTN)],
            [KeyboardButton(text=ATTACH_BUSINESS_BTN)],
        ],
        resize_keyboard=True,
    )
