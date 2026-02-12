from aiogram import Router, types
from aiogram.filters import Command
from datetime import datetime, timedelta
from sqlalchemy import select

from src.config import settings
from src.database.engine import async_session
from src.database.models import User
from src.poweron.service import PowerService
from src.poweron.utils import format_schedule, format_date_ua, get_current_status
from src.telegram.utils import get_main_keyboard
from src.logger import setup_logger

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
                f"• /tomorrow - графік на завтра\n",
                reply_markup=get_main_keyboard(),
                parse_mode="Markdown"
            )
        else:
            await message.answer(
                f"З поверненням! 👋\n\n"
                f"Використовуйте кнопки або просто напишіть сьогодні або завтра",
                reply_markup=get_main_keyboard()
            )


@router.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "ℹ️ **Допомога**\n\n"
        "**Доступні команди:**\n"
        "• /today - графік на сьогодні\n"
        "• /tomorrow - графік на завтра\n"
        "• /start - перезапустити бота\n\n"
        "Також ви можете просто написати сьогодні або завтра\n\n"
        "**Позначення:**\n"
        "🟢 Світло є\n"
        "🔴 Немає світла\n"
        "🟡 Перемикання\n\n"
        "💡 Графіки оновлюються автоматично кожні 10 хвилин",
        parse_mode="Markdown"
    )


async def send_schedule(message: types.Message, date: datetime, date_str: str):
    try:
        service = PowerService()
        date_display = format_date_ua(date)

        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.chat_id == message.from_user.id)
            )
            user = result.scalar_one_or_none()
            user_group = user.group if user else settings.DEFAULT_GROUP

        cached_times = await service.get_schedule_from_cache(date_str, user_group)

        # if not cached_times:
        #     logger.info(f"No cache for {date_str}, trying to fetch from API...")
        #     await service.get_schedule()
        #     cached_times = await service.get_schedule_from_cache(date_str, user_group)

        if cached_times:
            readable_text = format_schedule(cached_times)

            current_status_text = ""
            now = datetime.now()

            if date.date() == now.date():
                current_status = get_current_status(cached_times)
                if current_status:
                    current_status_text = f"⚡️ **Зараз:** {current_status}\n"

            caption = (
                f"📅 **Графік на {date_display}**\n"
                f"🏘 Група: **{user_group}**\n"
                f"{current_status_text}"
                f"⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
                f"{readable_text}\n"
                f"⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
                f"💡 _Оновлено о {now.strftime('%H:%M')}_"
            )

            await message.answer(caption, parse_mode="Markdown")
        else:
            await message.answer(
                f"❌ Графік на **{date_display}** ще не опублікований\n\n"
                f"Спробуйте пізніше або зачекайте автоматичного оновлення",
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.error(f"Error in send_schedule for {date_str}: {e}")
        await message.answer(
            "❌ Помилка при отриманні графіка\n\n"
            "Спробуйте ще раз за кілька хвилин"
        )


@router.message(Command("today"))
async def get_today_schedule(message: types.Message):
    today = datetime.now()
    today_date = today.strftime("%Y-%m-%d")
    await send_schedule(message, today, today_date)


@router.message(Command("tomorrow"))
async def get_tomorrow_schedule(message: types.Message):
    tomorrow = datetime.now() + timedelta(days=1)
    tomorrow_date = tomorrow.strftime("%Y-%m-%d")
    await send_schedule(message, tomorrow, tomorrow_date)


@router.message(lambda msg: msg.text and msg.text.lower() in ["допомога", "help"])
async def button_today(message: types.Message):
    await cmd_help(message)


@router.message(lambda msg: msg.text and msg.text.lower() in ["📅 сьогодні", "сьогодні"])
async def button_today(message: types.Message):
    await get_today_schedule(message)


@router.message(lambda msg: msg.text and msg.text.lower() in ["🔜 завтра", "завтра"])
async def button_tomorrow(message: types.Message):
    await get_tomorrow_schedule(message)
