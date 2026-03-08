import asyncio
import logging
import os
import re
import requests
from datetime import datetime

from aiogram import Bot, Dispatcher, F

from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    FSInputFile
)
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from flask import Flask
from threading import Thread

from sqlalchemy import String, BigInteger, DateTime, Enum, select
from openpyxl import Workbook
import enum

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
)

load_dotenv()
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")

if not TOKEN:
    raise ValueError("BOT_TOKEN not found")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL not found")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# =========================
# DATABASE SETUP
# =========================

engine = create_async_engine(DATABASE_URL)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class LeadStatus(enum.Enum):
    new = "new"
    in_progress = "in_progress"
    closed = "closed"
    rejected = "rejected"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(100))
    activity: Mapped[str] = mapped_column(String(255))
    inn: Mapped[str] = mapped_column(String(12))
    status: Mapped[LeadStatus] = mapped_column(
        Enum(LeadStatus),
        default=LeadStatus.new
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow
    )
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


Base = declarative_base()

# ===== ФУНКЦИЯ ПОИСКА ТЕНДЕРОВ =====

def search_tenders(keyword):

    url = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html"

    params = {
        "searchString": keyword
    }

    try:
        response = requests.get(url, params=params, timeout=10)

        if response.status_code != 200:
            return ["Не удалось получить данные о тендерах"]

        text = response.text

        tenders = []

        lines = text.split("\n")

        for line in lines:
            if "zakupki-card-item__title" in line:
                clean = re.sub("<.*?>", "", line).strip()

                if len(clean) > 10:
                    tenders.append(clean)

            if len(tenders) >= 5:
                break

        if not tenders:
            return ["Подходящие тендеры не найдены"]

        return tenders

    except Exception:
        return ["Ошибка при поиске тендеров"]


# =========================
# FSM
# =========================

class Form(StatesGroup):
    waiting_for_activity = State()
    waiting_for_inn = State()


start_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать", callback_data="start_onboarding")],
        [InlineKeyboardButton(text="📊 Получить тендеры", callback_data="get_tenders")]
    ]
)


main_menu = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📊 Получить тендеры", callback_data="get_tenders")],
        [InlineKeyboardButton(text="📞 Бесплатный разбор", callback_data="free_audit")]
    ]
)


@dp.message(Command("start"))
async def start_handler(message: Message):
    await message.answer(
        "Добро пожаловать в Tender Assist 👋\n\n"
        "Мы помогаем находить и сопровождать тендеры.",
        reply_markup=start_keyboard
    )


@dp.callback_query(F.data == "start_onboarding")
async def onboarding_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("В какой сфере работает ваша компания?")
    await state.set_state(Form.waiting_for_activity)
    await callback.answer()


@dp.callback_query(F.data == "get_tenders")
async def get_tenders(callback: CallbackQuery):

    async with SessionLocal() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )

        user = result.scalar()

    if not user:
        await callback.message.answer(
            "Сначала заполните анкету через /start"
        )
        await callback.answer()
        return

    await callback.message.answer("🔎 Ищем тендеры...")

    tenders = search_tenders(user.activity)

    text = "📊 Найдены тендеры:\n\n"

    for tender in tenders:
        text += f"• {tender}\n"

    await callback.message.answer(text)

    await callback.answer()


@dp.message(Form.waiting_for_activity)
async def process_activity(message: Message, state: FSMContext):
    await state.update_data(activity=message.text)
    await message.answer("Введите ИНН компании (10 или 12 цифр):")
    await state.set_state(Form.waiting_for_inn)


@dp.message(Form.waiting_for_inn)
async def process_inn(message: Message, state: FSMContext):
    inn = message.text.strip()

    if not re.fullmatch(r"\d{10}|\d{12}", inn):
        await message.answer("❌ ИНН должен содержать 10 или 12 цифр.")
        return

    await state.update_data(inn=inn)
    data = await state.get_data()

    # =========================
    # SAVE TO DATABASE
    # =========================
    async with SessionLocal() as session:
        new_user = User(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            activity=data["activity"],
            inn=data["inn"],
        )
        session.add(new_user)
        await session.commit()

    # =========================
    # SEND TO ADMIN
    # =========================
    await bot.send_message(
        ADMIN_ID,
        f"🆕 Новый лид!\n\n"
        f"Сфера: {data['activity']}\n"
        f"ИНН: {data['inn']}\n"
        f"Username: @{message.from_user.username}\n"
        f"Telegram ID: {message.from_user.id}"
    )

    await message.answer(
        "✅ Данные сохранены в системе.",
        reply_markup=main_menu
    )

    await state.clear()


@dp.callback_query(F.data == "get_tenders")
async def get_tenders(callback: CallbackQuery):
    await callback.message.answer("🔍 Мы подбираем для вас актуальные тендеры.")
    await callback.answer()


@dp.callback_query(F.data == "free_audit")
async def free_audit(callback: CallbackQuery):
    await callback.message.answer("📞 Наш специалист свяжется с вами.")
    await callback.answer()

from sqlalchemy import select, func


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


@dp.callback_query(F.data.startswith("status_"))
async def change_status(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    _, user_id, new_status = callback.data.split("_")

    async with SessionLocal() as session:
        result = await session.execute(
            select(User).where(User.id == int(user_id))
        )
        user = result.scalar()

        if not user:
            await callback.answer("Лид не найден", show_alert=True)
            return

        user.status = LeadStatus[new_status]
        await session.commit()

    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ Статус обновлён: {new_status}"
    )


    await callback.answer("Статус обновлён")
@dp.message(Command("leads"))
async def get_leads(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        return

    async with SessionLocal() as session:
        result = await session.execute(
            select(User).order_by(User.created_at.desc()).limit(10)
        )
        users = result.scalars().all()

    if not users:
        await message.answer("Лидов пока нет.")
        return

    for user in users:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🟡 В работе",
                        callback_data=f"status_{user.id}_in_progress"
                    ),
                    InlineKeyboardButton(
                        text="🟢 Закрыт",
                        callback_data=f"status_{user.id}_closed"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🔴 Неинтересен",
                        callback_data=f"status_{user.id}_rejected"
                    ),
                ]
            ]
        )

        text = (
            f"ID: {user.id}\n"
            f"Сфера: {user.activity}\n"
            f"ИНН: {user.inn}\n"
            f"Username: @{user.username}\n"
            f"Статус: {user.status.value}\n"
            f"Дата: {user.created_at}\n"
        )

        await message.answer(text, reply_markup=keyboard)
@dp.message(Command("export"))
async def export_leads(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        return

    async with SessionLocal() as session:
        result = await session.execute(
            select(User).order_by(User.created_at.desc())
        )
        users = result.scalars().all()

    if not users:
        await message.answer("Лидов пока нет.")
        return

    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"

    ws.append([
        "ID",
        "Telegram ID",
        "Username",
        "Сфера",
        "ИНН",
        "Статус",
        "Дата создания"
    ])

    for user in users:
        ws.append([
            user.id,
            user.telegram_id,
            user.username,
            user.activity,
            user.inn,
            user.status.value,
            str(user.created_at)
        ])

    file_name = "leads.xlsx"
    wb.save(file_name)

    file = FSInputFile(file_name)
    await message.answer_document(file)

@dp.message(Command("stats"))
async def get_stats(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        return

    async with SessionLocal() as session:
        result = await session.execute(
            select(func.count()).select_from(User)
        )
        total = result.scalar()

    await message.answer(f"📊 Всего лидов в базе: {total}")

async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    Thread(target=run_web).start()
    asyncio.run(main())
