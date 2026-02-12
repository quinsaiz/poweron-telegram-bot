from aiogram import Router, types
from aiogram.filters import Command
from datetime import datetime, timedelta
from sqlalchemy import select

from src.config import settings
from src.logger import setup_logger
from src.database.engine import async_session
from src.database.models import User
from src.poweron.service import PowerService
from src.telegram.utils import get_main_keyboard

router = Router()
logger = setup_logger(__name__, settings.LOG_LEVEL)


@router.message(Command("start"))
async def cmd_start(message: types.Message):
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.chat_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()

        if not user:
            new_user = User(chat_id=message.from_user.id, group=settings.DEFAULT_GROUP)
            session.add(new_user)
            await session.commit()
            await message.answer(
                f"👋 Вітаю!\n\n"
                f"🏘 Ваша група: **{settings.DEFAULT_GROUP}**\n\n"
                f"Використовуйте кнопки нижче або команди:\n"
                f"• /today - графік на сьогодні\n"
                f"• /tomorrow - графік на завтра\n"
                "Також ви можете просто написати сьогодні або завтра\n",
                reply_markup=get_main_keyboard(),
                parse_mode="Markdown"
            )
        else:
            await message.answer(
                f"З поверненням! 👋\n\n"
                f"Використовуйте кнопки або просто напишіть **сьогодні** або **завтра**",
                reply_markup=get_main_keyboard(),
                parse_mode="Markdown"
            )


@router.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "ℹ️ **Допомога**\n\n"
        "**Доступні команди:**\n"
        "• /today - графік на сьогодні\n"
        "• /tomorrow - графік на завтра\n"
        "Також ви можете просто написати **сьогодні** або **завтра**\n\n"
        "**Позначення:**\n"
        "🟢 Світло є\n"
        "🔴 Немає світла\n"
        "🟡 Перемикання\n\n"
        "💡 Графіки оновлюються автоматично кожні 10 хвилин",
        parse_mode="Markdown"
    )


@router.message(Command("today"))
async def get_today_schedule(message: types.Message):
    service = PowerService()
    text, _ = await service.get_formatted_schedule(message.from_user.id, datetime.now())
    await message.answer(text, parse_mode="Markdown")


@router.message(Command("tomorrow"))
async def get_tomorrow_schedule(message: types.Message):
    service = PowerService()
    tomorrow = datetime.now() + timedelta(days=1)
    text, _ = await service.get_formatted_schedule(message.from_user.id, tomorrow)
    await message.answer(text, parse_mode="Markdown")


@router.message(lambda msg: msg.text and msg.text.lower() in ["допомога", "help"])
async def button_today(message: types.Message):
    await cmd_help(message)


@router.message(lambda msg: msg.text and msg.text.lower() in ["📅 сьогодні", "сьогодні"])
async def button_today(message: types.Message):
    await get_today_schedule(message)


@router.message(lambda msg: msg.text and msg.text.lower() in ["🔜 завтра", "завтра"])
async def button_tomorrow(message: types.Message):
    await get_tomorrow_schedule(message)
