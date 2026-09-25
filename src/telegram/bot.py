from aiogram import Bot, Dispatcher

from src.config import settings

bot = Bot(token=settings.BOT_TOKEN.get_secret_value())
dp = Dispatcher()
