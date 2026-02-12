from aiogram.types import KeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder


def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="📅 Сьогодні"), KeyboardButton(text="🔜 Завтра"))

    return builder.as_markup(resize_keyboard=True)
