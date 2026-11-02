import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@akoshsaved").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/akoshsaved").strip()
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram-webhook").strip("/")
PORT = int(os.getenv("PORT", "10000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot_data.sqlite3")

PER_PAGE = 10
SEARCH_URL = "https://itunes.apple.com/search"
SEARCH_LIMIT = 50

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("music-bot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable kiritilmagan.")
if not RENDER_EXTERNAL_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL Render Web Service tomonidan berilishi kerak.")
if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
    raise RuntimeError("WEBHOOK_SECRET kiriting (kamida 16 belgi).")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# Har bir foydalanuvchining oxirgi qidiruv natijalari xotirada saqlanadi.
user_results: dict[int, list[dict]] = {}
sleep_until: datetime | None = None


def db():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            query TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    connection.commit()
    return connection


def record_user(user_id: int):
    now = datetime.now(timezone.utc).isoformat()
    with closing(db()) as conn:
        conn.execute(
            """INSERT INTO users(user_id, first_seen, last_seen)
               VALUES(?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen""",
            (user_id, now, now),
        )
        conn.commit()


def record_search(user_id: int, query: str):
    record_user(user_id)
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO searches(user_id, query, created_at) VALUES(?, ?, ?)",
            (user_id, query[:200], datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_stats():
    with closing(db()) as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        searches = conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
        today = datetime.now(timezone.utc).date().isoformat()
        today_searches = conn.execute(
            "SELECT COUNT(*) FROM searches WHERE substr(created_at, 1, 10)=?",
            (today,),
        ).fetchone()[0]
    return users, searches, today_searches


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def sub_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Kanalga obuna bo‘lish", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_sub")],
    ])


async def is_subscribed(user_id: int) -> bool:
    if not CHANNEL_USERNAME:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        if member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }:
            return True
        return (
            member.status == ChatMemberStatus.RESTRICTED
            and bool(getattr(member, "is_member", False))
        )
    except Exception:
        logger.exception("Kanal obunasini tekshirishda xato")
        return False


async def search_tracks(query: str) -> list[dict]:
    params = {
        "term": query,
        "entity": "song",
        "limit": SEARCH_LIMIT,
        "country": "us",
    }
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(SEARCH_URL, params=params) as response:
            response.raise_for_status()
            data = await response.json()
            return data.get("results", [])


def results_keyboard(items: list[dict], page: int):
    start = page * PER_PAGE
    rows = []
    for index, track in enumerate(items[start:start + PER_PAGE], start=start):
        title = track.get("trackName", "Noma’lum")
        artist = track.get("artistName", "Noma’lum")
        rows.append([
            InlineKeyboardButton(
                text=f"{index + 1}. {title} — {artist}"[:60],
                callback_data=f"song:{index}",
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️ Oldingi", callback_data=f"page:{page - 1}"
        ))
    if start + PER_PAGE < len(items):
        nav.append(InlineKeyboardButton(
            text="Keyingi ➡️", callback_data=f"page:{page + 1}"
        ))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_results(message: Message, user_id: int, page: int):
    items = user_results.get(user_id, [])
    pages = (len(items) + PER_PAGE - 1) // PER_PAGE
    if not items or page < 0 or page >= pages:
        await message.answer("Natijalar topilmadi. Qayta qidirib ko‘ring.")
        return

    first = page * PER_PAGE + 1
    last = min((page + 1) * PER_PAGE, len(items))
    await message.answer(
        f"🎵 <b>Musiqalar topildi</b>\n"
        f"🔎 Natijalar: {first}–{last} / {len(items)}\n\n"
        "Kerakli qo‘shiqni tanlang:",
        reply_markup=results_keyboard(items, page),
    )


async def user_can_use(message: Message) -> bool:
    global sleep_until
    if sleep_until and datetime.now(timezone.utc) < sleep_until:
        if not is_admin(message.from_user.id):
            until = sleep_until.strftime("%H:%M UTC")
            await message.answer(f"⏸ Bot vaqtincha tanaffusda. Qayta urinib ko‘ring: {until}")
            return False
    elif sleep_until:
        sleep_until = None

    if not await is_subscribed(message.from_user.id):
        await message.answer(
            "🎵 Botdan foydalanish uchun avval kanalimizga obuna bo‘ling:",
            reply_markup=sub_keyboard(),
        )
        return False
    record_user(message.from_user.id)
    return True


@dp.message(CommandStart())
async def start_handler(message: Message):
    if not await user_can_use(message):
        return
    await message.answer(
        "👋 <b>Music Search Bot'ga xush kelibsiz!</b>\n\n"
        "🎧 Qo‘shiq yoki ijrochi nomini yuboring.\n"
        "Masalan: <i>Jaloliddin Ahmadaliyev</i>\n\n"
        "ℹ️ Buyruqlar: /help"
    )


@dp.message(Command("help"))
async def help_handler(message: Message):
    await message.answer(
        "<b>Botdan foydalanish</b>\n"
        "• Qo‘shiq yoki ijrochi nomini yozing.\n"
        "• Natijalardan kerakli qo‘shiqni tanlang.\n"
        "• Rasmiy tinglash havolasini oching.\n\n"
        "<b>Buyruqlar</b>\n"
        "/start — botni boshlash\n"
        "/help — yordam"
        + ("\n/stats — statistika\n/sleep 10 — 10 daqiqaga pauza\n/wake — pauzani bekor qilish"
           if is_admin(message.from_user.id) else "")
    )


@dp.callback_query(F.data == "check_sub")
async def check_sub_handler(call: CallbackQuery):
    if await is_subscribed(call.from_user.id):
        record_user(call.from_user.id)
        await call.message.answer("✅ Obuna tasdiqlandi! Endi qo‘shiq nomini yuboring.")
        await call.answer("Obuna tasdiqlandi!")
    else:
        await call.answer("Avval kanalga obuna bo‘ling!", show_alert=True)


@dp.message(Command("stats"))
async def stats_handler(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Bu buyruq faqat admin uchun.")
        return
    users, searches, today_searches = get_stats()
    await message.answer(
        "📊 <b>Bot statistikasi</b>\n"
        f"👥 Foydalanuvchilar: {users}\n"
        f"🔎 Jami qidiruvlar: {searches}\n"
        f"📅 Bugungi qidiruvlar (UTC): {today_searches}"
    )


@dp.message(Command("sleep"))
async def sleep_handler(message: Message):
    global sleep_until
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Bu buyruq faqat admin uchun.")
        return
    parts = (message.text or "").split(maxsplit=1)
    try:
        minutes = int(parts[1]) if len(parts) > 1 else 10
        if not 1 <= minutes <= 1440:
            raise ValueError
    except ValueError:
        await message.answer("Foydalanish: /sleep 10 (1–1440 daqiqa)")
        return
    sleep_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    await message.answer(f"⏸ Bot {minutes} daqiqaga pauzaga qo‘yildi.")


@dp.message(Command("wake"))
async def wake_handler(message: Message):
    global sleep_until
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Bu buyruq faqat admin uchun.")
        return
    sleep_until = None
    await message.answer("▶️ Bot yana ishlashga tayyor.")


@dp.message(F.text)
async def search_handler(message: Message):
    if not await user_can_use(message):
        return

    query = (message.text or "").strip()
    if query.startswith("/"):
        return
    if len(query) < 2:
        await message.answer("Qo‘shiq yoki ijrochi nomini yozing.")
        return

    status = await message.answer("🔎 Musiqa qidirilmoqda...")
    try:
        items = await search_tracks(query)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.exception("Musiqa qidiruv API xatosi")
        await status.edit_text("❌ Qidiruv xizmatida xatolik. Keyinroq urinib ko‘ring.")
        return
    except Exception:
        logger.exception("Kutilmagan qidiruv xatosi")
        await status.edit_text("❌ Kutilmagan xatolik yuz berdi.")
        return

    if not items:
        await status.edit_text("😕 Bu nom bo‘yicha musiqa topilmadi.")
        return

    user_results[message.from_user.id] = items
    record_search(message.from_user.id, query)
    await status.delete()
    await show_results(message, message.from_user.id, 0)


@dp.callback_query(F.data.startswith("page:"))
async def page_handler(call: CallbackQuery):
    if not await is_subscribed(call.from_user.id):
        await call.message.answer(
            "Kanalga obuna bo‘ling:", reply_markup=sub_keyboard()
        )
        await call.answer()
        return

    try:
        page = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Sahifa noto‘g‘ri.")
        return

    items = user_results.get(call.from_user.id, [])
    pages = (len(items) + PER_PAGE - 1) // PER_PAGE
    if page < 0 or page >= pages:
        await call.answer("Qidiruv muddati tugagan. Qayta qidiring.", show_alert=True)
        return

    first = page * PER_PAGE + 1
    last = min((page + 1) * PER_PAGE, len(items))
    await call.message.edit_text(
        f"🎵 <b>Musiqalar topildi</b>\n"
        f"Natijalar: {first}–{last} / {len(items)}\n\n"
        "Kerakli qo‘shiqni tanlang:",
        reply_markup=results_keyboard(items, page),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("song:"))
async def song_handler(call: CallbackQuery):
    if not await is_subscribed(call.from_user.id):
        await call.message.answer(
            "Kanalga obuna bo‘ling:", reply_markup=sub_keyboard()
        )
        await call.answer()
        return

    items = user_results.get(call.from_user.id, [])
    try:
        index = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Tanlov noto‘g‘ri.")
        return
    if index < 0 or index >= len(items):
        await call.answer("Natija topilmadi. Qayta qidiring.", show_alert=True)
        return

    track = items[index]
    title = escape(track.get("trackName", "Noma’lum"))
    artist = escape(track.get("artistName", "Noma’lum"))
    album = escape(track.get("collectionName", "Noma’lum"))
    duration_ms = track.get("trackTimeMillis")
    duration = ""
    if isinstance(duration_ms, int) and duration_ms > 0:
        seconds = duration_ms // 1000
        duration = f"\n⏱ Davomiyligi: {seconds // 60}:{seconds % 60:02d}"

    caption = (
        f"🎵 <b>{title}</b>\n"
        f"🎤 Ijrochi: {artist}\n"
        f"💿 Albom: {album}{duration}\n\n"
        "🔗 Rasmiy tinglash sahifasi:"
    )

    buttons = []
    link = track.get("trackViewUrl")
    preview = track.get("previewUrl")
    if link:
        buttons.append([InlineKeyboardButton(text="🎧 Rasmiy tinglash", url=link)])
    if preview:
        buttons.append([InlineKeyboardButton(text="▶️ 30 soniyalik preview", url=preview)])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    artwork = track.get("artworkUrl100")
    try:
        if artwork:
            await call.message.answer_photo(
                photo=artwork, caption=caption, reply_markup=keyboard
            )
        else:
            await call.message.answer(caption, reply_markup=keyboard)
    except Exception:
        logger.exception("Trek ma’lumotini yuborishda xato")
        await call.message.answer(caption, reply_markup=keyboard)
    await call.answer()


async def health(request: web.Request):
    return web.json_response({"status": "ok", "service": "music-bot"})


async def on_startup(app: web.Application):
    webhook_url = f"{RENDER_EXTERNAL_URL}/{WEBHOOK_PATH}"
    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=True,
    )
    logger.info("Webhook sozlandi: %s", webhook_url)


async def on_shutdown(app: web.Application):
    try:
        await bot.session.close()
    except Exception:
        logger.exception("Bot session yopilishida xato")


def create_app():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    webhook_handler.register(app, path=f"/{WEBHOOK_PATH}")
    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


if __name__ == "__main__":
    application = create_app()
    web.run_app(application, host="0.0.0.0", port=PORT)
