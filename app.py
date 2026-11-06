import asyncio
import logging
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import urlparse
import html

from typing import Any, Awaitable, Callable

from aiohttp import web, ClientSession, ClientTimeout
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    FSInputFile, InlineKeyboardMarkup,
    InlineKeyboardButton, CallbackQuery
)
from aiogram.webhook.aiohttp_server import (
    SimpleRequestHandler, setup_application
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("asaved")

# =====================================
# SOZLAMALAR
# =====================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook").strip("/")
PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

CHANNEL_USERNAME = "@kinoolamiuzzzz"
CHANNEL_URL = "https://t.me/kinoolamiuzzzz"
CHANNEL_TITLE = "🎬 Kino Olami"

DB_PATH = os.getenv("DB_PATH", "asaved_users.db")

AD_TEXT = (
    "🚀 <b>KANALINGIZNI KO‘PROQ INSONLARGA TANITING!</b>\n\n"
    "Telegram kanalingiz rivojlanishini xohlaysizmi?\n\n"
    "📣 ASAVED orqali reklama joylashtiring!\n\n"
    "👤 Admin: @akramovx7\n"
    '<a href="https://t.me/akramovx7">💬 Murojaat qilish</a>'
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN Environment Variable kiritilmagan")

if not WEBHOOK_SECRET or not re.fullmatch(
    r"[A-Za-z0-9_-]{16,256}", WEBHOOK_SECRET
):
    raise RuntimeError("WEBHOOK_SECRET noto‘g‘ri")

if not BASE_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL topilmadi")

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()
router = Router()
dp.include_router(router)

# =====================================
# TUGMALAR
# =====================================

BTN_ROUND = "🎥 Dumaloq video"
BTN_LINK = "🔗 Video havolasi"
BTN_916 = "📱 9:16 format"
BTN_TTS = "🎙️ Matnni ovozga aylantirish"
BTN_MALE = "👨 Erkak ovozi"
BTN_FEMALE = "👩 Ayol ovozi"

BTN_MOVIE_SEARCH = "🎬 Kino qidirish"
BTN_ADD_MOVIE = "➕ Kino qo‘shish"
ADMIN_BROADCAST_BTN = "📢 Reklamani hammaga yuborish"

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_MOVIE_SEARCH)],
        [KeyboardButton(text=BTN_ROUND), KeyboardButton(text=BTN_916)],
        [KeyboardButton(text=BTN_LINK)],
        [KeyboardButton(text=BTN_TTS)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Kerakli funksiyani tanlang"
)

subscribe_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🎬 Kino Olami kanaliga obuna bo‘lish",
                url=CHANNEL_URL
            )
        ],
        [
            InlineKeyboardButton(
                text="✅ Obunani tekshirish",
                callback_data="check_subscription"
            )
        ]
    ]
)

voice_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text=BTN_MALE),
            KeyboardButton(text=BTN_FEMALE)
        ],
        [KeyboardButton(text="⬅️ Asosiy menyu")],
    ],
    resize_keyboard=True
)

# =====================================
# UMUMIY SOZLAMALAR
# =====================================

user_modes: dict[int, str] = {}

# Adminlarning kino qo‘shish jarayoni
movie_uploads: dict[int, dict] = {}

subscription_cache: dict[int, float] = {}
SUBSCRIPTION_CACHE_SECONDS = 30

VIDEO_SEMAPHORE = asyncio.Semaphore(2)
TTS_SEMAPHORE = asyncio.Semaphore(2)

MAX_TTS_CHARS = 3000

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()

TTS_MODEL_ID = os.getenv(
    "ELEVENLABS_MODEL_ID",
    "eleven_multilingual_v2"
).strip()

TTS_VOICES = {
    "male": os.getenv(
        "ELEVENLABS_MALE_VOICE_ID",
        "pNInz6obpgDQGcFmaJgB"
    ).strip(),
    "female": os.getenv(
        "ELEVENLABS_FEMALE_VOICE_ID",
        "21m00Tcm4TlvDq8ikWAM"
    ).strip(),
}

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
}

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 49 * 1024 * 1024
MAX_LINK_BYTES = 49 * 1024 * 1024
MAX_SECONDS = 600

_admin_ids_raw = os.getenv("ADMIN_IDS", "") or os.getenv("ADMIN_ID", "")

ADMIN_IDS = {
    int(x.strip())
    for x in _admin_ids_raw.replace(";", ",").split(",")
    if x.strip().isdigit()
}

# =====================================
# DATABASE
# =====================================

def init_user_db():
    db_file = Path(DB_PATH)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH, timeout=30) as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS ad_sent_users (
                user_id INTEGER PRIMARY KEY,
                sent_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id INTEGER PRIMARY KEY,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            INSERT OR IGNORE INTO bot_users (user_id)
            SELECT user_id FROM ad_sent_users
        """)

        # Kino bazasi
        conn.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                movie_number INTEGER UNIQUE NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                file_id TEXT NOT NULL,
                media_type TEXT DEFAULT 'video',
                poster_file_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()


def register_user(user_id: int):
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("""
            INSERT INTO bot_users (user_id)
            VALUES (?)
            ON CONFLICT(user_id)
            DO UPDATE SET last_seen=CURRENT_TIMESTAMP
        """, (user_id,))
        conn.commit()


def get_all_user_ids() -> list[int]:
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        rows = conn.execute(
            "SELECT user_id FROM bot_users"
        ).fetchall()

    return [int(row[0]) for row in rows]


def mark_ad_sent_once(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO ad_sent_users (user_id) VALUES (?)",
            (user_id,)
        )
        conn.commit()
        return cursor.rowcount == 1


async def send_ad_once(chat_id: int, user_id: int):
    try:
        if not mark_ad_sent_once(user_id):
            return

        await bot.send_message(
            chat_id,
            AD_TEXT,
            disable_web_page_preview=True
        )

    except Exception:
        log.exception("Reklama yuborishda xatolik")

        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.execute(
                "DELETE FROM ad_sent_users WHERE user_id = ?",
                (user_id,)
            )
            conn.commit()


# =====================================
# KINO DATABASE FUNKSIYALARI
# =====================================

def save_movie(
    number: int,
    title: str,
    description: str,
    file_id: str,
    media_type: str,
    poster_file_id: str | None
):
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("""
            INSERT INTO movies (
                movie_number,
                title,
                description,
                file_id,
                media_type,
                poster_file_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(movie_number)
            DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                file_id=excluded.file_id,
                media_type=excluded.media_type,
                poster_file_id=excluded.poster_file_id
        """, (
            number,
            title,
            description,
            file_id,
            media_type,
            poster_file_id
        ))
        conn.commit()


def search_movies(query: str) -> list[tuple]:
    query = query.strip()

    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row

        if query.isdigit():
            rows = conn.execute("""
                SELECT id, movie_number, title, description
                FROM movies
                WHERE movie_number = ?
                LIMIT 10
            """, (int(query),)).fetchall()

            if rows:
                return [tuple(row) for row in rows]

        rows = conn.execute("""
            SELECT id, movie_number, title, description
            FROM movies
            WHERE title LIKE ?
            ORDER BY movie_number
            LIMIT 10
        """, (f"%{query}%",)).fetchall()

        return [tuple(row) for row in rows]


def get_movie_by_id(movie_id: int):
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row

        row = conn.execute("""
            SELECT *
            FROM movies
            WHERE id = ?
        """, (movie_id,)).fetchone()

        return dict(row) if row else None


def delete_movie(number: int) -> bool:
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cursor = conn.execute(
            "DELETE FROM movies WHERE movie_number = ?",
            (number,)
        )
        conn.commit()
        return cursor.rowcount > 0


async def send_movie(chat_id: int, movie: dict):
    title = html.escape(movie["title"])
    description = html.escape(movie.get("description") or "")

    caption = f"🎬 <b>{title}</b>"

    if description:
        caption += f"\n\n{description}"

    poster_id = movie.get("poster_file_id")

    if poster_id:
        await bot.send_photo(
            chat_id,
            poster_id,
            caption=caption[:1024]
        )

        caption = None

    if movie["media_type"] == "document":
        await bot.send_document(
            chat_id,
            movie["file_id"],
            caption=caption
        )
    else:
        await bot.send_video(
            chat_id,
            movie["file_id"],
            caption=caption,
            supports_streaming=True
        )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# =====================================
# ASOSIY MENYU
# =====================================

def main_text() -> str:
    return (
        "Assalomu alaykum! <b>ASAVED</b> botiga xush kelibsiz.\n\n"
        "🎬 <b>Kino qidirish</b> — kino nomi yoki raqamini yozing.\n"
        "🎥 <b>Dumaloq video</b> — galereyadan 60 soniyagacha video yuboring.\n"
        "📱 <b>9:16 format</b> — videoni 9:16 formatga o‘tkazing.\n"
        "🔗 <b>Video havolasi</b> — YouTube, Instagram yoki TikTok havolasini yuboring.\n"
        "🎙️ <b>Matnni ovozga aylantirish</b> — matnni AI ovozida MP3 qiling.\n\n"
        "Kerakli tugmani tanlang."
    )


def admin_reply_keyboard() -> ReplyKeyboardMarkup:
    rows = [list(row) for row in keyboard.keyboard]

    rows.append([KeyboardButton(text=BTN_ADD_MOVIE)])
    rows.append([KeyboardButton(text=ADMIN_BROADCAST_BTN)])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True
    )


# =====================================
# KINO OLAMI OBUNASINI TEKSHIRISH
# =====================================

async def is_subscribed(
    user_id: int,
    use_cache: bool = True
) -> bool:

    now = asyncio.get_running_loop().time()
    cached_until = subscription_cache.get(user_id, 0)

    if use_cache and cached_until > now:
        return True

    try:
        member = await bot.get_chat_member(
            CHANNEL_USERNAME,
            user_id
        )

        subscribed = member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }

        if not subscribed:
            subscription_cache.pop(user_id, None)
            return False

        subscription_cache[user_id] = (
            now + SUBSCRIPTION_CACHE_SECONDS
        )

        return True

    except Exception:
        log.exception("Obunani tekshirishda xatolik")
        subscription_cache.pop(user_id, None)
        return False


async def send_subscription_required(message: Message):
    await message.answer(
        "⛔ <b>ASAVED botidan foydalanish uchun "
        "Kino Olami kanaliga obuna bo‘ling!</b>\n\n"
        "1️⃣ Kanalga kiring.\n"
        "2️⃣ Obuna bo‘ling.\n"
        "3️⃣ «✅ Obunani tekshirish» tugmasini bosing.",
        reply_markup=subscribe_keyboard
    )


# =====================================
# OBUNA MIDDLEWARE
# =====================================

class SubscriptionMiddleware(BaseMiddleware):

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:

        if not event.from_user:
            return await handler(event, data)

        register_user(event.from_user.id)

        if event.text and event.text.split()[0].split("@")[0] == "/myid":
            return await handler(event, data)

        if await is_subscribed(event.from_user.id):
            await send_ad_once(
                event.chat.id,
                event.from_user.id
            )
            return await handler(event, data)

        await send_subscription_required(event)
        return None


router.message.middleware(SubscriptionMiddleware())


# =====================================
# OBUNANI TEKSHIRISH
# =====================================

@router.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: CallbackQuery):

    user_id = callback.from_user.id
    register_user(user_id)

    subscribed = await is_subscribed(
        user_id,
        use_cache=False
    )

    if subscribed:
        subscription_cache[user_id] = (
            asyncio.get_running_loop().time()
            + SUBSCRIPTION_CACHE_SECONDS
        )

        await callback.answer("✅ Obuna tasdiqlandi!")

        if callback.message:
            await send_ad_once(
                callback.message.chat.id,
                user_id
            )

            await callback.message.answer(
                main_text(),
                reply_markup=(
                    admin_reply_keyboard()
                    if is_admin(user_id)
                    else keyboard
                )
            )

    else:
        await callback.answer(
            "❌ Avval kanalga obuna bo‘ling!",
            show_alert=True
        )


# =====================================
# /MYID
# =====================================

@router.message(Command("myid"))
async def show_my_id(message: Message):

    if message.from_user:
        await message.answer(
            f"🆔 Telegram ID: <code>{message.from_user.id}</code>\n\n"
            "Bu raqamni Render → Environment → ADMIN_IDS ga kiriting."
        )


# =====================================
# ADMIN PANEL
# =====================================

@router.message(Command("admin"))
async def admin_panel(message: Message):

    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Sizda admin huquqi yo‘q.")
        return

    await message.answer(
        "🛠 <b>Admin panel</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{len(get_all_user_ids())}</b>\n"
        "🎬 Kino qo‘shish uchun tegishli tugmani bosing.",
        reply_markup=admin_reply_keyboard()
    )


# =====================================
# KINO QO‘SHISH
# =====================================

@router.message(Command("addmovie"))
@router.message(F.text == BTN_ADD_MOVIE)
async def add_movie_start(message: Message):

    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Bu funksiya faqat admin uchun.")
        return

    movie_uploads[message.from_user.id] = {
        "step": "number"
    }

    await message.answer(
        "➕ <b>Yangi kino qo‘shish</b>\n\n"
        "Avval kino uchun raqam yuboring.\n"
        "Masalan: <code>6</code>\n\n"
        "Bekor qilish uchun /cancel yozing."
    )


@router.message(Command("cancel"))
async def cancel_movie_upload(message: Message):

    if message.from_user:
        movie_uploads.pop(message.from_user.id, None)

    await message.answer("❌ Amal bekor qilindi.")


@router.message(Command("delmovie"))
async def delete_movie_command(message: Message):

    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo‘q.")
        return

    parts = (message.text or "").split(maxsplit=1)

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(
            "Ishlatish: <code>/delmovie 6</code>"
        )
        return

    number = int(parts[1])

    if delete_movie(number):
        await message.answer(
            f"🗑 {number}-raqamli kino o‘chirildi."
        )
    else:
        await message.answer("Bunday raqamli kino topilmadi.")


@router.message(F.photo)
async def receive_movie_poster(message: Message):

    user_id = message.from_user.id if message.from_user else 0
    upload = movie_uploads.get(user_id)

    if not upload or upload.get("step") != "poster":
        return

    upload["poster_file_id"] = message.photo[-1].file_id
    upload["step"] = "video"

    await message.answer(
        "✅ Poster saqlandi!\n\n"
        "Endi filmni video yoki fayl ko‘rinishida yuboring.\n"
        "Katta film bo‘lsa, kanalga yuklab, botga forward qiling."
    )


# =====================================
# ADMIN KINO YUKLASH MATN BOSQICHLARI
# =====================================

def get_video_info(message: Message):

    if message.video:
        return (
            message.video.file_id,
            message.video.file_size,
            message.video.duration,
            "video"
        )

    if (
        message.document
        and (message.document.mime_type or "").startswith("video/")
    ):
        return (
            message.document.file_id,
            message.document.file_size,
            None,
            "document"
        )

    return None


# =====================================
# START
# =====================================

@router.message(CommandStart())
async def start(message: Message):

    user_modes.pop(message.from_user.id, None)

    await message.answer(
        main_text(),
        reply_markup=(
            admin_reply_keyboard()
            if is_admin(message.from_user.id)
            else keyboard
        )
    )


# =====================================
# KINO QIDIRISH TUGMASI
# =====================================

@router.message(F.text == BTN_MOVIE_SEARCH)
async def movie_search_start(message: Message):

    user_modes[message.from_user.id] = "movie_search"

    await message.answer(
        "🎬 <b>Kino qidirish</b>\n\n"
        "Kino nomini yoki raqamini yuboring.\n"
        "Masalan: <code>6</code>"
    )


# =====================================
# KINO TANLASH CALLBACK
# =====================================

@router.callback_query(F.data.startswith("movie:"))
async def movie_selected(callback: CallbackQuery):

    if not await is_subscribed(callback.from_user.id, use_cache=False):
        await callback.answer(
            "Avval Kino Olami kanaliga obuna bo‘ling.",
            show_alert=True
        )
        return

    try:
        movie_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Kino raqami noto‘g‘ri.")
        return

    movie = get_movie_by_id(movie_id)

    if not movie:
        await callback.answer("Kino topilmadi.", show_alert=True)
        return

    await callback.answer("🎬 Kino yuborilmoqda...")

    try:
        await send_movie(callback.from_user.id, movie)
    except Exception:
        log.exception("Kino yuborishda xatolik")
        if callback.message:
            await callback.message.answer(
                "❌ Kinoni yuborib bo‘lmadi. Keyinroq urinib ko‘ring."
            )


# =====================================
# ADMIN REKLAMA
# =====================================

@router.message(F.text == ADMIN_BROADCAST_BTN)
async def admin_broadcast_start(message: Message):

    if not message.from_user or not is_admin(message.from_user.id):
        return

    total = len(get_all_user_ids())

    confirm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Ha, hammaga yuborish",
                    callback_data="admin_broadcast_confirm"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Bekor qilish",
                    callback_data="admin_broadcast_cancel"
                )
            ]
        ]
    )

    await message.answer(
        f"📢 Reklama <b>{total}</b> ta foydalanuvchiga yuboriladi.\n"
        "Davom etasizmi?",
        reply_markup=confirm_kb
    )


@router.callback_query(F.data == "admin_broadcast_cancel")
async def admin_broadcast_cancel(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo‘q.", show_alert=True)
        return

    await callback.answer("Bekor qilindi.")

    if callback.message:
        await callback.message.edit_text(
            "❌ Reklama yuborish bekor qilindi."
        )


@router.callback_query(F.data == "admin_broadcast_confirm")
async def admin_broadcast_confirm(callback: CallbackQuery):

    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo‘q.", show_alert=True)
        return

    await callback.answer("Yuborish boshlandi.")

    if callback.message:
        await callback.message.edit_text(
            "⏳ Reklama yuborilmoqda..."
        )

    user_ids = get_all_user_ids()
    sent = 0
    failed = 0

    for user_id in user_ids:
        try:
            await bot.send_message(user_id, AD_TEXT)
            sent += 1
        except Exception:
            failed += 1

        await asyncio.sleep(0.05)

    if callback.message:
        await callback.message.answer(
            "✅ <b>Reklama yuborish yakunlandi!</b>\n\n"
            f"👥 Jami: {len(user_ids)}\n"
            f"✅ Yuborildi: {sent}\n"
            f"❌ Yuborilmadi: {failed}",
            reply_markup=admin_reply_keyboard()
        )


# =====================================
# VIDEO FUNKSIYALARINI TANLASH
# =====================================

@router.message(F.text == BTN_ROUND)
async def choose_round(message: Message):

    user_modes[message.from_user.id] = "round"

    await message.answer(
        "🎥 Galereyadan 60 soniyagacha video yuboring."
    )


@router.message(F.text == BTN_916)
async def choose_916(message: Message):

    user_modes[message.from_user.id] = "916"

    await message.answer(
        "📱 9:16 formatga o‘tkazish uchun videoni yuboring."
    )


@router.message(F.text == BTN_LINK)
async def choose_link(message: Message):

    user_modes[message.from_user.id] = "link"

    await message.answer(
        "🔗 YouTube, Instagram yoki TikTok havolasini yuboring."
    )


# =====================================
# TTS TANLASH
# =====================================

@router.message(F.text == BTN_TTS)
async def choose_tts(message: Message):

    user_modes[message.from_user.id] = "voice_choose"

    await message.answer(
        "🎙️ Qaysi ElevenLabs ovozida matnni MP3 qilamiz?",
        reply_markup=voice_keyboard
    )


@router.message(F.text == BTN_MALE)
async def choose_tts_male(message: Message):

    user_modes[message.from_user.id] = "tts_male"

    await message.answer(
        "👨 Erkak ovozi tanlandi. Endi matn yuboring.",
        reply_markup=(
            admin_reply_keyboard()
            if is_admin(message.from_user.id)
            else keyboard
        )
    )


@router.message(F.text == BTN_FEMALE)
async def choose_tts_female(message: Message):

    user_modes[message.from_user.id] = "tts_female"

    await message.answer(
        "👩 Ayol ovozi tanlandi. Endi matn yuboring.",
        reply_markup=(
            admin_reply_keyboard()
            if is_admin(message.from_user.id)
            else keyboard
        )
    )


@router.message(F.text == "⬅️ Asosiy menyu")
async def back_to_menu(message: Message):

    user_modes.pop(message.from_user.id, None)

    await message.answer(
        main_text(),
        reply_markup=(
            admin_reply_keyboard()
            if is_admin(message.from_user.id)
            else keyboard
        )
    )


# =====================================
# ADMIN KINO YUKLASH MATN BOSQICHLARI
# =====================================

@router.message(F.text)
async def handle_text(message: Message):

    text = (message.text or "").strip()
    user_id = message.from_user.id
    mode = user_modes.get(user_id)

    # Admin kino qo‘shish jarayoni
    upload = movie_uploads.get(user_id)

    if upload:

        step = upload.get("step")

        if step == "number":

            if not text.isdigit() or int(text) < 1:
                await message.answer(
                    "❌ Faqat musbat raqam yuboring. Masalan: 6"
                )
                return

            upload["number"] = int(text)
            upload["step"] = "title"

            await message.answer("🎬 Kino nomini yuboring.")
            return

        if step == "title":

            if len(text) > 150:
                await message.answer("Kino nomi 150 belgidan oshmasin.")
                return

            upload["title"] = text
            upload["step"] = "description"

            await message.answer(
                "📝 Kino haqida qisqacha tavsif yuboring.\n"
                "Tavsif bo‘lmasa, /skip yozing."
            )
            return

        if step == "description":

            upload["description"] = "" if text == "/skip" else text[:800]
            upload["step"] = "poster"

            await message.answer(
                "🖼 Kino posterini yuboring.\n\n"
                "Poster bo‘lmasa, /skip yozing."
            )
            return

        if step == "poster":

            if text == "/skip":
                upload["poster_file_id"] = None
                upload["step"] = "video"

                await message.answer(
                    "Endi filmni video yoki fayl qilib yuboring.\n"
                    "Katta film bo‘lsa, kanalga yuklab, botga forward qiling."
                )
            else:
                await message.answer(
                    "Poster rasmini yuboring yoki /skip yozing."
                )
            return

        if step == "video":

            await message.answer(
                "🎞 Endi film videosini yuboring yoki kanalga yuklangan filmni forward qiling."
            )
            return

    # Kino qidirish
    if mode == "movie_search":

        if not text:
            await message.answer("Kino nomi yoki raqamini yuboring.")
            return

        results = search_movies(text)
        user_modes.pop(user_id, None)

        if not results:
            await message.answer(
                "😕 Bu nom yoki raqam bo‘yicha kino topilmadi."
            )
            return

        buttons = []

        for movie_id, number, title, description in results:
            buttons.append([
                InlineKeyboardButton(
                    text=f"🎬 {number}. {title[:45]}",
                    callback_data=f"movie:{movie_id}"
                )
            ])

        await message.answer(
            f"🔎 <b>{len(results)} ta natija topildi.</b>\n"
            "Kerakli kinoni tanlang:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=buttons
            )
        )
        return

    # TTS
    if mode in ("tts_male", "tts_female"):
        await process_tts(message, text, mode)
        return

    if mode == "voice_choose":
        await message.answer(
            "Avval erkak yoki ayol ovozini tanlang.",
            reply_markup=voice_keyboard
        )
        return

    # Link yuklash
    if mode == "link":

        if not valid_video_url(text):
            await message.answer(
                "❌ HTTPS YouTube, Instagram yoki TikTok havolasini yuboring."
            )
            return

        status = await message.answer(
            "⚡ Video yuklanmoqda..."
        )

        try:
            platform, platform_title = detect_platform(text)

            async with VIDEO_SEMAPHORE:
                with tempfile.TemporaryDirectory(
                    prefix="asaved_link_"
                ) as tmp:

                    video_path = await download_public_link(
                        text,
                        Path(tmp),
                        platform
                    )

                    await message.answer_video(
                        FSInputFile(video_path),
                        caption=f"✅ {platform_title} videosi tayyor!"
                    )

            await status.delete()

        except Exception:
            log.exception("Link download failed")
            await status.edit_text(
                "❌ Videoni yuklab bo‘lmadi. Havola yopiq yoki video hajmi katta bo‘lishi mumkin."
            )

        finally:
            user_modes.pop(user_id, None)

        return

    if mode in ("round", "916"):
        await message.answer(
            "Iltimos, havola emas, galereyadan video yuboring."
        )
        return

    # Raqam yozib ham kino qidirish
    if text.isdigit():
        results = search_movies(text)

        if results:
            buttons = []

            for movie_id, number, title, description in results:
                buttons.append([
                    InlineKeyboardButton(
                        text=f"🎬 {number}. {title[:45]}",
                        callback_data=f"movie:{movie_id}"
                    )
                ])

            await message.answer(
                "🎬 Kino topildi. Tanlang:",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=buttons
                )
            )
            return

    if valid_video_url(text):
        await message.answer(
            "Avval 🔗 Video havolasi tugmasini bosing."
        )
    else:
        await message.answer(
            "Menyudan kerakli funksiyani tanlang.",
            reply_markup=(
                admin_reply_keyboard()
                if is_admin(user_id)
                else keyboard
            )
        )


# =====================================
# ELEVENLABS TTS
# =====================================

async def text_to_speech(
    text: str,
    voice_id: str,
    destination: Path
):

    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY topilmadi")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
    }

    payload = {
        "text": text,
        "model_id": TTS_MODEL_ID,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
        },
    }

    timeout = ClientTimeout(total=120, connect=20)

    async with ClientSession(timeout=timeout) as session:
        async with session.post(
            url,
            headers=headers,
            json=payload
        ) as response:

            if response.status != 200:
                detail = (await response.text())[:500]

                log.error(
                    "ElevenLabs xatosi: %s %s",
                    response.status,
                    detail
                )

                if response.status == 401:
                    raise RuntimeError("ElevenLabs API kaliti noto‘g‘ri")

                if response.status == 429:
                    raise RuntimeError("ElevenLabs limiti tugagan bo‘lishi mumkin")

                raise RuntimeError(
                    f"ElevenLabs API xatosi ({response.status})"
                )

            audio_data = await response.read()

    if not audio_data:
        raise RuntimeError("Audio bo‘sh qaytdi")

    destination.write_bytes(audio_data)


async def process_tts(
    message: Message,
    text: str,
    mode: str
):

    if len(text) > MAX_TTS_CHARS:
        await message.answer(
            f"Matn eng ko‘pi {MAX_TTS_CHARS} ta belgi bo‘lishi kerak."
        )
        return

    if not text.strip():
        await message.answer("Matn yuboring.")
        return

    voice_key = "male" if mode == "tts_male" else "female"
    voice = TTS_VOICES[voice_key]

    status = await message.answer(
        "🎙️ Matn ovozga aylantirilmoqda..."
    )

    try:
        async with TTS_SEMAPHORE:
            with tempfile.TemporaryDirectory(
                prefix="asaved_tts_"
            ) as tmp:

                audio_path = Path(tmp) / "asaved_voice.mp3"

                await text_to_speech(
                    text,
                    voice,
                    audio_path
                )

                await message.answer_audio(
                    FSInputFile(audio_path),
                    title="ASAVED AI ovoz",
                    caption=(
                        "✅ Tayyor! "
                        f"{'Erkak' if voice_key == 'male' else 'Ayol'} AI ovozi"
                    )
                )

        await status.delete()

    except Exception:
        log.exception("TTS generation failed")

        await status.edit_text(
            "❌ Ovoz yaratishda xatolik bo‘ldi. Keyinroq urinib ko‘ring."
        )

    finally:
        user_modes.pop(message.from_user.id, None)


# =====================================
# VIDEO YUKLASH VA MA'LUMOTLAR
# =====================================

async def download_telegram_video(
    message: Message,
    target: Path
) -> tuple[Path, int | None]:

    info = get_video_info(message)

    if not info:
        raise ValueError("Video topilmadi.")

    file_id, size, duration, media_type = info

    if size and size > MAX_UPLOAD_BYTES:
        raise ValueError("Video 20 MB dan katta.")

    if duration and duration > MAX_SECONDS:
        raise ValueError("Video 10 daqiqadan uzun.")

    tg_file = await bot.get_file(file_id)

    await bot.download_file(
        tg_file.file_path,
        destination=target
    )

    return target, duration


# =====================================
# VIDEO FUNKSIYALARI
# =====================================

async def run_ffmpeg(args: list[str]):

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            stderr.decode("utf-8", "ignore")[-1200:]
            or "FFmpeg xatoligi"
        )


async def make_round(src: Path, dst: Path):

    await run_ffmpeg([
        "-i", str(src),
        "-t", "60",
        "-vf",
        "scale=640:640:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=640:640,setsar=1",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-threads", "0",
        "-crf", "27",
        "-c:a", "aac",
        "-b:a", "96k",
        "-ac", "2",
        "-movflags", "+faststart",
        str(dst)
    ])


async def make_916(src: Path, dst: Path):

    await run_ffmpeg([
        "-i", str(src),
        "-vf",
        "scale=720:1280:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=720:1280,setsar=1",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-threads", "0",
        "-crf", "26",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(dst)
    ])


async def process_uploaded_video(
    message: Message,
    mode: str
):

    status = await message.answer(
        "⚡ Video tayyorlanmoqda..."
    )

    try:
        async with VIDEO_SEMAPHORE:
            with tempfile.TemporaryDirectory(
                prefix="asaved_"
            ) as tmp:

                folder = Path(tmp)
                src = folder / "input_video"
                dst = folder / "output.mp4"

                await download_telegram_video(message, src)

                if mode == "round":
                    await make_round(src, dst)

                    if dst.stat().st_size > MAX_OUTPUT_BYTES:
                        raise ValueError("Tayyor video juda katta.")

                    await message.answer_video_note(
                        FSInputFile(dst)
                    )

                else:
                    await make_916(src, dst)

                    if dst.stat().st_size > MAX_OUTPUT_BYTES:
                        raise ValueError("Tayyor video 49 MB dan katta.")

                    await message.answer_video(
                        FSInputFile(dst),
                        caption="📱 9:16 format tayyor!"
                    )

        await status.delete()

    except Exception as exc:
        log.exception("Video processing failed")

        await status.edit_text(
            f"❌ Xatolik: {html.escape(str(exc)[:700])}"
        )

    finally:
        user_modes.pop(message.from_user.id, None)


# =====================================
# VIDEO HAVOLASI
# =====================================

def valid_video_url(raw: str) -> bool:

    try:
        parsed = urlparse(raw.strip())
        host = (parsed.hostname or "").lower()

        return parsed.scheme == "https" and (
            host in ALLOWED_HOSTS
            or any(host.endswith("." + h) for h in ALLOWED_HOSTS)
        )

    except Exception:
        return False


def detect_platform(url: str) -> tuple[str, str]:

    host = (urlparse(url).hostname or "").lower()

    if "instagram.com" in host:
        return "instagram", "Instagram"

    if "tiktok.com" in host:
        return "tiktok", "TikTok"

    return "youtube", "YouTube"


async def download_public_link(
    url: str,
    folder: Path,
    platform: str | None = None
) -> Path:

    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp o‘rnatilmagan.")

    platform = platform or detect_platform(url)[0]
    output_template = str(folder / "link_video.%(ext)s")

    options = {
        "format": "bestvideo+bestaudio/best",
        "format_sort": ["res", "quality", "fps"],
        "outtmpl": output_template,
        "noplaylist": True,
        "max_filesize": MAX_LINK_BYTES,
        "socket_timeout": 15,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "concurrent_fragment_downloads": 4,
        "buffersize": 1024 * 1024,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "remuxvideo": "mp4",
    }

    def do_download():
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

    await asyncio.to_thread(do_download)

    files = [
        p for p in folder.iterdir()
        if p.is_file() and p.name.startswith("link_video.")
    ]

    if not files:
        raise RuntimeError("Video topilmadi.")

    result = max(files, key=lambda p: p.stat().st_size)

    if result.stat().st_size > MAX_LINK_BYTES:
        raise RuntimeError("Video 49 MB dan katta.")

    return result


# =====================================
# VIDEO FAYLNI QABUL QILISH
# =====================================

@router.message(F.video | F.document)
async def handle_video(message: Message):

    user_id = message.from_user.id if message.from_user else 0

    # Admin kino qo‘shayotgan bo‘lsa
    upload = movie_uploads.get(user_id)

    if upload and upload.get("step") == "video":

        info = get_video_info(message)

        if not info:
            await message.answer(
                "❌ Video fayl yuboring. Oddiy hujjat bo‘lsa, uning turi video bo‘lishi kerak."
            )
            return

        file_id, size, duration, media_type = info

        try:
            save_movie(
                number=upload["number"],
                title=upload["title"],
                description=upload.get("description", ""),
                file_id=file_id,
                media_type=media_type,
                poster_file_id=upload.get("poster_file_id")
            )

            number = upload["number"]
            title = upload["title"]

            movie_uploads.pop(user_id, None)

            await message.answer(
                "✅ <b>Kino bazaga saqlandi!</b>\n\n"
                f"🔢 Raqami: <b>{number}</b>\n"
                f"🎬 Nomi: <b>{html.escape(title)}</b>\n\n"
                "Endi foydalanuvchilar «🎬 Kino qidirish» orqali topa oladi.",
                reply_markup=admin_reply_keyboard()
            )

        except Exception:
            log.exception("Kino saqlashda xatolik")
            await message.answer("❌ Kinoni bazaga saqlashda xatolik.")
        return

    mode = user_modes.get(user_id)

    if mode not in ("round", "916"):
        await message.answer(
            "Avval 🎥 Dumaloq video yoki 📱 9:16 format tugmasini tanlang."
        )
        return

    if not get_video_info(message):
        await message.answer("Iltimos, video fayl yuboring.")
        return

    await process_uploaded_video(message, mode)


# =====================================
# BOSHQA XABARLAR
# =====================================

@router.message()
async def fallback(message: Message):

    await message.answer(
        "Menyudan kerakli funksiyani tanlang.",
        reply_markup=(
            admin_reply_keyboard()
            if message.from_user and is_admin(message.from_user.id)
            else keyboard
        )
    )


# =====================================
# WEBHOOK
# =====================================

async def on_startup():

    init_user_db()

    webhook_url = f"{BASE_URL}/{WEBHOOK_PATH}"

    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types()
    )

    log.info("Webhook set: %s", webhook_url)


async def on_shutdown():

    await bot.delete_webhook(
        drop_pending_updates=False
    )

    await bot.session.close()


async def main():

    init_user_db()

    app = web.Application(
        client_max_size=2 * 1024 * 1024
    )

    app.router.add_get(
        "/",
        lambda request: web.Response(
            text="ASAVED bot is running"
        )
    )

    app.router.add_get(
        "/health",
        lambda request: web.Response(text="ok")
    )

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET
    ).register(
        app,
        path=f"/{WEBHOOK_PATH}"
    )

    setup_application(
        app,
        dp,
        bot=bot
    )

    app.on_startup.append(
        lambda app: on_startup()
    )

    app.on_cleanup.append(
        lambda app: on_shutdown()
    )

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    log.info("ASAVED listening on port %s", PORT)

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
