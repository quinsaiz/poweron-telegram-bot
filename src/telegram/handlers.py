from datetime import timedelta

from aiogram import F, Router, types
from aiogram.filters import Command
from sqlalchemy import select

from src.database.engine import async_session
from src.database.models import User
from src.domain_time import kyiv_now
from src.poweron.service import PowerService
from src.telegram.utils import get_main_keyboard

router = Router()


@router.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    if not message.from_user:
        return

    async with async_session() as session:
        result = await session.execute(select(User).where(User.chat_id == message.from_user.id))
        user = result.scalar_one_or_none()

        is_new_user = user is None
        if is_new_user:
            session.add(User(chat_id=message.from_user.id))
            await session.commit()

    service = PowerService()
    group = await service.group_resolver.ensure_group()
    greeting = "👋 Вітаю!" if is_new_user else "З поверненням! 👋"
    if group is None:
        await message.answer(
            f"{greeting}\n\n⚠️ Інформація про групу тимчасово недоступна. Спробуйте пізніше.",
            reply_markup=get_main_keyboard(),
        )
    else:
        await message.answer(
            f"{greeting}\n\n"
            f"🏘 Ваша група визначена автоматично: **{group}**\n\n"
            "Використовуйте кнопки нижче або команди:\n"
            "• /today - графік на сьогодні\n"
            "• /tomorrow - графік на завтра\n"
            "Також ви можете просто написати сьогодні або завтра\n",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown",
        )


@router.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
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
        parse_mode="Markdown",
    )


@router.message(Command("today"))
async def get_today_schedule(message: types.Message) -> None:
    if not message.from_user:
        return

    service = PowerService()
    text, _ = await service.get_formatted_schedule(message.from_user.id, kyiv_now())
    await message.answer(text, parse_mode="Markdown")


@router.message(Command("tomorrow"))
async def get_tomorrow_schedule(message: types.Message) -> None:
    if not message.from_user:
        return

    service = PowerService()
    tomorrow = kyiv_now() + timedelta(days=1)
    text, _ = await service.get_formatted_schedule(message.from_user.id, tomorrow)
    await message.answer(text, parse_mode="Markdown")


@router.message(F.text.lower().in_(["допомога", "help"]))
async def text_help(message: types.Message) -> None:
    await cmd_help(message)


@router.message(F.text.lower().in_(["📅 сьогодні", "сьогодні"]))
async def text_today(message: types.Message) -> None:
    await get_today_schedule(message)


@router.message(F.text.lower().in_(["🔜 завтра", "завтра"]))
async def text_tomorrow(message: types.Message) -> None:
    await get_tomorrow_schedule(message)
