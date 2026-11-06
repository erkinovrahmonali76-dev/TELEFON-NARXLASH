import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from contextlib import suppress
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.webhook.aiohttp_server import (
    SimpleRequestHandler,
    setup_application,
)

# =========================================================
# SOZLAMALAR
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook").strip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "asaved-secret").strip()

CHANNEL_USERNAME = "@ReklamaBozoriUzz"
CHANNEL_URL = "https://t.me/ReklamaBozoriUzz"

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "")).split(",")
    if x.strip().isdigit()
}

PRO_CARD = os.getenv("PRO_CARD", "KARTA RAQAMINI KIRITING")
PRO_CARDHOLDER = os.getenv("PRO_CARDHOLDER", "KARTA EGASI")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_MODEL_ID = os.getenv(
    "ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"
)

MALE_VOICE_ID = os.getenv(
    "ELEVENLABS_MALE_VOICE_ID", "pNInz6obpgDQGcFmaJgB"
)
FEMALE_VOICE_ID = os.getenv(
    "ELEVENLABS_FEMALE_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"
)

DB_PATH = os.getenv("DB_PATH", "asaved.db")

MAX_VIDEO_SIZE = 49 * 1024 * 1024
MAX_TG_UPLOAD = 49 * 1024 * 1024
MAX_TTS_LENGTH = 3000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("ASAVED")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN Render Environment Variables ichida yo‘q!")

if not BASE_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL Render Environment Variables ichida yo‘q!")

# =========================================================
# BOT
# =========================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()

# Foydalanuvchilarning vaqtinchalik holatlari
user_modes = {}
waiting_payment = set()
waiting_tts_text = set()
waiting_batch_links = set()
waiting_quality = {}

# Bir vaqtda ishlaydigan yuklashlar soni
VIDEO_SEMAPHORE = asyncio.Semaphore(2)

# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pro_subscriptions (
                user_id INTEGER PRIMARY KEY,
                expires_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pro_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                plan TEXT,
                amount INTEGER,
                file_id TEXT,
                file_hash TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)


def save_user(user_id, username, first_name):
    with db() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name, joined_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (
            user_id,
            username or "",
            first_name or "",
            datetime.now(timezone.utc).isoformat()
        ))


def is_pro(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT expires_at FROM pro_subscriptions WHERE user_id=?",
            (user_id,)
        ).fetchone()

    if not row:
        return False

    try:
        expires = datetime.fromisoformat(row["expires_at"])
        return expires > datetime.now(timezone.utc)
    except Exception:
        return False


def activate_pro(user_id, days):
    now = datetime.now(timezone.utc)

    with db() as conn:
        row = conn.execute(
            "SELECT expires_at FROM pro_subscriptions WHERE user_id=?",
            (user_id,)
        ).fetchone()

        start = now

        if row:
            try:
                old_expiry = datetime.fromisoformat(row["expires_at"])
                if old_expiry > now:
                    start = old_expiry
            except Exception:
                pass

        expires = start + timedelta(days=days)

        conn.execute("""
            INSERT INTO pro_subscriptions (user_id, expires_at)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                expires_at=excluded.expires_at
        """, (user_id, expires.isoformat()))

    return expires


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🎥 Dumaloq video"),
                KeyboardButton(text="📱 9:16 format")
            ],
            [
                KeyboardButton(text="🔗 Video havolasi")
            ],
            [
                KeyboardButton(text="🎙️ Matnni ovozga aylantirish")
            ],
            [
                KeyboardButton(text="💎 PRO")
            ],
            [
                KeyboardButton(text="🎵 PRO MP3"),
                KeyboardButton(text="📚 PRO video navbati")
            ]
        ],
        resize_keyboard=True
    )


def subscription_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📢 Kanalga obuna bo‘lish",
                url=CHANNEL_URL
            )
        ],
        [
            InlineKeyboardButton(
                text="✅ Obunani tekshirish",
                callback_data="check_sub"
            )
        ]
    ])


def pro_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="7 kun — 20 000 so‘m",
                callback_data="buy_pro_7"
            )
        ],
        [
            InlineKeyboardButton(
                text="30 kun — 35 000 so‘m",
                callback_data="buy_pro_30"
            )
        ],
        [
            InlineKeyboardButton(
                text="👤 PRO holatim",
                callback_data="my_pro"
            )
        ]
    ])


# =========================================================
# OBUNA TEKSHIRISH
# =========================================================

async def check_subscription(user_id):
    try:
        member = await bot.get_chat_member(
            CHANNEL_USERNAME,
            user_id
        )

        return member.status in (
            "creator",
            "administrator",
            "member"
        )

    except Exception:
        log.exception("Obunani tekshirishda xato")
        return False


async def require_subscription(message):
    user_id = message.from_user.id

    if await check_subscription(user_id):
        return True

    await message.answer(
        "Botdan foydalanish uchun kanalimizga obuna bo‘ling:",
        reply_markup=subscription_keyboard()
    )
    return False


# =========================================================
# YORDAMCHI FUNKSIYALAR
# =========================================================

def is_admin(user_id):
    return user_id in ADMIN_IDS


def valid_video_url(url):
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()

        allowed = (
            "youtube.com",
            "youtu.be",
            "instagram.com",
            "tiktok.com",
            "vm.tiktok.com"
        )

        return any(
            host == domain or host.endswith("." + domain)
            for domain in allowed
        )

    except Exception:
        return False


def get_video_download(url, output, quality=None, audio=False):
    import yt_dlp

    if audio:
        fmt = "bestaudio/best"
    elif quality:
        fmt = (
            f"bestvideo[height<={quality}]+bestaudio/"
            f"best[height<={quality}]/best"
        )
    else:
        fmt = "best[filesize<49000000]/best"

    options = {
        "format": fmt,
        "outtmpl": output,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_VIDEO_SIZE,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
    }

    if audio:
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192"
        }]

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])


async def download_link(url, output, quality=None, audio=False):
    async with VIDEO_SEMAPHORE:
        await asyncio.to_thread(
            get_video_download,
            url,
            output,
            quality,
            audio
        )


async def run_ffmpeg(args):
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE
    )

    _, stderr = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(
            stderr.decode(errors="ignore")[-1500:]
        )


def file_size(path):
    return os.path.getsize(path) if os.path.exists(path) else 0


# =========================================================
# START
# =========================================================

@dp.message(CommandStart())
async def start_handler(message: Message):
    user = message.from_user

    save_user(
        user.id,
        user.username,
        user.first_name
    )

    if not await require_subscription(message):
        return

    await message.answer(
        "👋 <b>ASAVED botga xush kelibsiz!</b>\n\n"
        "Kerakli xizmatni menyudan tanlang.",
        reply_markup=main_keyboard()
    )


@dp.callback_query(F.data == "check_sub")
async def check_sub_callback(callback: CallbackQuery):
    if await check_subscription(callback.from_user.id):
        await callback.message.edit_text(
            "✅ Obuna tasdiqlandi. Botdan foydalanishingiz mumkin."
        )
        await callback.message.answer(
            "Asosiy menyu:",
            reply_markup=main_keyboard()
        )
    else:
        await callback.answer(
            "Avval kanalga obuna bo‘ling.",
            show_alert=True
        )


# =========================================================
# PRO
# =========================================================

@dp.message(F.text == "💎 PRO")
async def pro_menu(message: Message):
    if not await require_subscription(message):
        return

    await message.answer(
        "💎 <b>ASAVED PRO</b>\n\n"
        "PRO imkoniyatlari:\n"
        "• MP3 formatda yuklab olish\n"
        "• Bir nechta videoni navbatga qo‘yish\n"
        "• Video sifatini tanlash\n\n"
        "7 kun — 20 000 so‘m\n"
        "30 kun — 35 000 so‘m",
        reply_markup=pro_keyboard()
    )


@dp.callback_query(F.data == "my_pro")
async def my_pro_callback(callback: CallbackQuery):
    if is_pro(callback.from_user.id):
        with db() as conn:
            row = conn.execute(
                "SELECT expires_at FROM pro_subscriptions WHERE user_id=?",
                (callback.from_user.id,)
            ).fetchone()

        expires = datetime.fromisoformat(row["expires_at"])
        await callback.message.answer(
            f"💎 PRO faol.\nAmal qilish muddati: "
            f"<b>{expires.strftime('%Y-%m-%d %H:%M')}</b>"
        )
    else:
        await callback.message.answer(
            "Sizda hozir faol PRO obuna mavjud emas."
        )

    await callback.answer()


@dp.callback_query(F.data.in_({"buy_pro_7", "buy_pro_30"}))
async def buy_pro_callback(callback: CallbackQuery):
    if callback.data == "buy_pro_7":
        plan = "7 kun"
        amount = 20000
    else:
        plan = "30 kun"
        amount = 35000

    waiting_payment.add(callback.from_user.id)

    await callback.message.answer(
        f"💎 <b>PRO — {plan}</b>\n\n"
        f"To‘lov: <b>{amount:,} so‘m</b>\n\n"
        f"Karta: <code>{PRO_CARD}</code>\n"
        f"Karta egasi: <b>{PRO_CARDHOLDER}</b>\n\n"
        "To‘lovni amalga oshirib, chek rasmini shu botga yuboring."
    )

    user_modes[callback.from_user.id] = {
        "type": "payment",
        "plan": plan,
        "amount": amount
    }

    await callback.answer()


async def get_file_hash(file_id):
    with tempfile.TemporaryDirectory() as temp:
        path = os.path.join(temp, "receipt.jpg")
        await bot.download(file_id, destination=path)

        digest = hashlib.sha256(
            Path(path).read_bytes()
        ).hexdigest()

        return digest


@dp.message(F.photo)
async def photo_handler(message: Message):
    user_id = message.from_user.id

    if user_id not in waiting_payment:
        return

    mode = user_modes.get(user_id, {})

    if mode.get("type") != "payment":
        return

    file_id = message.photo[-1].file_id

    try:
        digest = await get_file_hash(file_id)
    except Exception:
        await message.answer("Chekni o‘qishda xatolik yuz berdi.")
        return

    with db() as conn:
        duplicate = conn.execute(
            "SELECT id FROM pro_payments WHERE file_hash=?",
            (digest,)
        ).fetchone()

        if duplicate:
            await message.answer(
                "Bu chek avval yuborilgan. Boshqa chek yuboring."
            )
            return

        cursor = conn.execute("""
            INSERT INTO pro_payments
            (user_id, username, plan, amount, file_id, file_hash, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """, (
            user_id,
            message.from_user.username or "",
            mode["plan"],
            mode["amount"],
            file_id,
            digest,
            datetime.now(timezone.utc).isoformat()
        ))

        payment_id = cursor.lastrowid

    waiting_payment.discard(user_id)
    user_modes.pop(user_id, None)

    await message.answer(
        "✅ Chekingiz adminga yuborildi.\n"
        "Tekshirilgach, PRO faollashtiriladi."
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Tasdiqlash",
                callback_data=f"approve_{payment_id}"
            ),
            InlineKeyboardButton(
                text="❌ Rad etish",
                callback_data=f"reject_{payment_id}"
            )
        ]
    ])

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(
                admin_id,
                file_id,
                caption=(
                    f"💎 PRO to‘lov cheki\n\n"
                    f"Foydalanuvchi: <code>{user_id}</code>\n"
                    f"Username: @{message.from_user.username or 'yo‘q'}\n"
                    f"Tarif: {mode['plan']}\n"
                    f"Summa: {mode['amount']:,} so‘m\n"
                    f"To‘lov ID: {payment_id}"
                ),
                reply_markup=keyboard
            )
        except Exception:
            log.exception("Adminga chek yuborilmadi")


@dp.callback_query(F.data.startswith("approve_"))
async def approve_payment(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo‘q.", show_alert=True)
        return

    payment_id = int(callback.data.split("_")[1])

    with db() as conn:
        payment = conn.execute(
            "SELECT * FROM pro_payments WHERE id=?",
            (payment_id,)
        ).fetchone()

        if not payment or payment["status"] != "pending":
            await callback.answer("Bu to‘lov allaqachon ko‘rib chiqilgan.")
            return

        days = 7 if payment["plan"] == "7 kun" else 30

        expires = activate_pro(
            payment["user_id"],
            days
        )

        conn.execute(
            "UPDATE pro_payments SET status='approved' WHERE id=?",
            (payment_id,)
        )

    try:
        await bot.send_message(
            payment["user_id"],
            "🎉 <b>PRO faollashtirildi!</b>\n\n"
            f"Tarif: {payment['plan']}\n"
            f"Amal qilish muddati: {expires.strftime('%Y-%m-%d %H:%M')}"
        )
    except Exception:
        pass

    await callback.message.edit_caption(
        (callback.message.caption or "") + "\n\n✅ TASDIQLANDI"
    )
    await callback.answer("PRO faollashtirildi.")


@dp.callback_query(F.data.startswith("reject_"))
async def reject_payment(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo‘q.", show_alert=True)
        return

    payment_id = int(callback.data.split("_")[1])

    with db() as conn:
        payment = conn.execute(
            "SELECT * FROM pro_payments WHERE id=?",
            (payment_id,)
        ).fetchone()

        if not payment or payment["status"] != "pending":
            await callback.answer("Bu to‘lov allaqachon ko‘rib chiqilgan.")
            return

        conn.execute(
            "UPDATE pro_payments SET status='rejected' WHERE id=?",
            (payment_id,)
        )

    try:
        await bot.send_message(
            payment["user_id"],
            "❌ To‘lov chekingiz rad etildi. "
            "Iltimos, to‘lov ma’lumotlarini tekshirib qayta yuboring."
        )
    except Exception:
        pass

    await callback.message.edit_caption(
        (callback.message.caption or "") + "\n\n❌ RAD ETILDI"
    )
    await callback.answer("To‘lov rad etildi.")


# =========================================================
# DUMALOQ VIDEO
# =========================================================

@dp.message(F.text == "🎥 Dumaloq video")
async def round_start(message: Message):
    if not await require_subscription(message):
        return

    user_modes[message.from_user.id] = {"type": "round"}

    await message.answer(
        "🎥 Dumaloq videoga aylantirish uchun videoni yuboring."
    )


# =========================================================
# 9:16 VIDEO
# =========================================================

@dp.message(F.text == "📱 9:16 format")
async def vertical_start(message: Message):
    if not await require_subscription(message):
        return

    user_modes[message.from_user.id] = {"type": "vertical"}

    await message.answer(
        "📱 9:16 formatga o‘tkazish uchun videoni yuboring."
    )


@dp.message(F.video)
async def video_handler(message: Message):
    user_id = message.from_user.id
    mode = user_modes.get(user_id, {})

    if mode.get("type") not in ("round", "vertical"):
        return

    if message.video.file_size and message.video.file_size > MAX_VIDEO_SIZE:
        await message.answer("Video hajmi juda katta. 49 MB dan kichik video yuboring.")
        return

    action = mode["type"]
    user_modes.pop(user_id, None)

    status = await message.answer("⏳ Video tayyorlanmoqda...")

    with tempfile.TemporaryDirectory() as temp:
        source = os.path.join(temp, "source.mp4")
        output = os.path.join(temp, "output.mp4")

        try:
            await bot.download(
                message.video.file_id,
                destination=source
            )

            if action == "round":
                await run_ffmpeg([
                    "-i", source,
                    "-vf",
                    "scale=640:640:force_original_aspect_ratio=increase,"
                    "crop=640:640",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-crf", "27",
                    "-c:a", "aac",
                    "-b:a", "96k",
                    "-t", "60",
                    output
                ])

                if file_size(output) > MAX_TG_UPLOAD:
                    await status.edit_text("Tayyor video hajmi juda katta.")
                    return

                await message.answer_video_note(
                    video_note=__import__(
                        "aiogram.types", fromlist=["FSInputFile"]
                    ).FSInputFile(output)
                )

            else:
                await run_ffmpeg([
                    "-i", source,
                    "-vf",
                    "scale=720:1280:force_original_aspect_ratio=increase,"
                    "crop=720:1280",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-crf", "26",
                    "-c:a", "aac",
                    "-b:a", "96k",
                    output
                ])

                if file_size(output) > MAX_TG_UPLOAD:
                    await status.edit_text("Tayyor video hajmi juda katta.")
                    return

                from aiogram.types import FSInputFile

                await message.answer_video(
                    FSInputFile(output),
                    caption="✅ 9:16 format tayyor."
                )

            await status.delete()

        except Exception:
            log.exception("Video qayta ishlashda xato")
            await status.edit_text(
                "❌ Videoni qayta ishlashda xatolik yuz berdi."
            )


# =========================================================
# HAVOLA ORQALI VIDEO
# =========================================================

@dp.message(F.text == "🔗 Video havolasi")
async def link_start(message: Message):
    if not await require_subscription(message):
        return

    user_modes[message.from_user.id] = {"type": "link"}

    await message.answer(
        "🔗 YouTube, Instagram yoki TikTok video havolasini yuboring."
    )


async def process_link(message, url, quality=None, audio=False):
    status = await message.answer("⏳ Yuklanmoqda...")

    with tempfile.TemporaryDirectory() as temp:
        output = os.path.join(
            temp,
            "audio.%(ext)s" if audio else "video.%(ext)s"
        )

        try:
            await download_link(
                url,
                output,
                quality=quality,
                audio=audio
            )

            files = list(Path(temp).glob("*"))

            if not files:
                await status.edit_text("Video topilmadi.")
                return

            path = str(files[0])

            if file_size(path) > MAX_TG_UPLOAD:
                await status.edit_text(
                    "❌ Fayl hajmi Telegram limitidan katta."
                )
                return

            from aiogram.types import FSInputFile

            if audio:
                await message.answer_audio(
                    FSInputFile(path),
                    caption="🎵 MP3 tayyor."
                )
            else:
                await message.answer_video(
                    FSInputFile(path),
                    caption="✅ Video tayyor."
                )

            await status.delete()

        except Exception:
            log.exception("Havoladan yuklashda xato")
            await status.edit_text(
                "❌ Video yuklanmadi. Havolani tekshiring yoki keyinroq urinib ko‘ring."
            )


@dp.message(F.text)
async def text_router(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()
    mode = user_modes.get(user_id, {})

    if mode.get("type") == "link":
        user_modes.pop(user_id, None)

        if not valid_video_url(text):
            await message.answer(
                "❌ YouTube, Instagram yoki TikTok havolasini yuboring."
            )
            return

        await process_link(message, text)
        return

    if mode.get("type") == "tts":
        user_modes.pop(user_id, None)
        await make_tts(message, text, mode.get("voice", "male"))
        return

    if mode.get("type") == "mp3":
        user_modes.pop(user_id, None)

        if not is_pro(user_id):
            await message.answer("Bu funksiya faqat PRO uchun.")
            return

        if not valid_video_url(text):
            await message.answer("Havola noto‘g‘ri.")
            return

        await process_link(message, text, audio=True)
        return

    if mode.get("type") == "batch":
        if not is_pro(user_id):
            user_modes.pop(user_id, None)
            await message.answer("Bu funksiya faqat PRO uchun.")
            return

        links = [
            x.strip()
            for x in text.splitlines()
            if x.strip()
        ]

        user_modes.pop(user_id, None)

        if not links or len(links) > 5:
            await message.answer("1 tadan 5 tagacha havola yuboring.")
            return

        for link in links:
            if not valid_video_url(link):
                await message.answer(f"❌ Noto‘g‘ri havola: {link}")
                continue

            await process_link(message, link)

        return

    if mode.get("type") == "tts_text":
        return


# =========================================================
# TTS
# =========================================================

@dp.message(F.text == "🎙️ Matnni ovozga aylantirish")
async def tts_start(message: Message):
    if not await require_subscription(message):
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="👨 Erkak ovozi",
                callback_data="tts_male"
            ),
            InlineKeyboardButton(
                text="👩 Ayol ovozi",
                callback_data="tts_female"
            )
        ]
    ])

    await message.answer(
        "Ovoz turini tanlang:",
        reply_markup=keyboard
    )


@dp.callback_query(F.data.in_({"tts_male", "tts_female"}))
async def tts_voice_callback(callback: CallbackQuery):
    voice = "male" if callback.data == "tts_male" else "female"

    user_modes[callback.from_user.id] = {
        "type": "tts",
        "voice": voice
    }

    await callback.message.answer(
        "🎙️ Ovozga aylantirish uchun matn yuboring. "
        "Maksimal 3000 belgi."
    )
    await callback.answer()


async def make_tts(message, text, voice):
    if not ELEVENLABS_API_KEY:
        await message.answer(
            "❌ ElevenLabs API kaliti sozlanmagan."
        )
        return

    if not text or len(text) > MAX_TTS_LENGTH:
        await message.answer("Matn 1–3000 belgi orasida bo‘lishi kerak.")
        return

    voice_id = MALE_VOICE_ID if voice == "male" else FEMALE_VOICE_ID

    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    )

    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg"
    }

    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL_ID,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75
        }
    }

    status = await message.answer("🎙️ Ovoz tayyorlanmoqda...")

    try:
        timeout = aiohttp.ClientTimeout(total=120)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                headers=headers,
                json=payload
            ) as response:

                if response.status != 200:
                    error = await response.text()
                    log.error("ElevenLabs: %s", error[:1000])

                    await status.edit_text(
                        "❌ Ovoz yaratilmadi. API kaliti yoki ovoz sozlamalarini tekshiring."
                    )
                    return

                audio_data = await response.read()

        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "voice.mp3")

            with open(path, "wb") as f:
                f.write(audio_data)

            from aiogram.types import FSInputFile

            await message.answer_audio(
                FSInputFile(path),
                caption="✅ Ovoz tayyor."
            )

        await status.delete()

    except Exception:
        log.exception("TTS xatosi")
        await status.edit_text("❌ Ovoz yaratishda xatolik yuz berdi.")


# =========================================================
# PRO MP3 VA BIR NECHTA VIDEO
# =========================================================

@dp.message(F.text == "🎵 PRO MP3")
async def pro_mp3_start(message: Message):
    if not await require_subscription(message):
        return

    if not is_pro(message.from_user.id):
        await message.answer(
            "🎵 MP3 yuklab olish faqat PRO foydalanuvchilar uchun."
        )
        return

    user_modes[message.from_user.id] = {"type": "mp3"}

    await message.answer(
        "MP3 formatga aylantirish uchun video havolasini yuboring."
    )


@dp.message(F.text == "📚 PRO video navbati")
async def pro_batch_start(message: Message):
    if not await require_subscription(message):
        return

    if not is_pro(message.from_user.id):
        await message.answer(
            "📚 Bu funksiya faqat PRO foydalanuvchilar uchun."
        )
        return

    user_modes[message.from_user.id] = {"type": "batch"}

    await message.answer(
        "1 tadan 5 tagacha video havolasini yuboring.\n"
        "Har bir havolani yangi qatordan yozing."
    )


# =========================================================
# ADMIN
# =========================================================

@dp.message(Command("myid"))
async def myid(message: Message):
    await message.answer(
        f"Sizning Telegram ID: <code>{message.from_user.id}</code>"
    )


@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return

    with db() as conn:
        users = conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        pending = conn.execute(
            "SELECT COUNT(*) FROM pro_payments WHERE status='pending'"
        ).fetchone()[0]

        active = conn.execute(
            "SELECT COUNT(*) FROM pro_subscriptions"
        ).fetchone()[0]

    await message.answer(
        "🛠 <b>ADMIN PANEL</b>\n\n"
        f"👥 Foydalanuvchilar: {users}\n"
        f"💎 PRO yozuvlari: {active}\n"
        f"⏳ Kutilayotgan to‘lovlar: {pending}"
    )


@dp.message(Command("prostats"))
async def pro_stats(message: Message):
    if not is_admin(message.from_user.id):
        return

    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM pro_payments"
        ).fetchone()[0]

        approved = conn.execute(
            "SELECT COUNT(*) FROM pro_payments WHERE status='approved'"
        ).fetchone()[0]

        pending = conn.execute(
            "SELECT COUNT(*) FROM pro_payments WHERE status='pending'"
        ).fetchone()[0]

        rejected = conn.execute(
            "SELECT COUNT(*) FROM pro_payments WHERE status='rejected'"
        ).fetchone()[0]

    await message.answer(
        "💎 <b>PRO STATISTIKA</b>\n\n"
        f"Jami to‘lovlar: {total}\n"
        f"Tasdiqlangan: {approved}\n"
        f"Kutilmoqda: {pending}\n"
        f"Rad etilgan: {rejected}"
    )


@dp.message(Command("prousers"))
async def pro_users(message: Message):
    if not is_admin(message.from_user.id):
        return

    with db() as conn:
        rows = conn.execute("""
            SELECT user_id, expires_at
            FROM pro_subscriptions
            ORDER BY expires_at DESC
            LIMIT 50
        """).fetchall()

    if not rows:
        await message.answer("PRO foydalanuvchilar yo‘q.")
        return

    lines = ["💎 <b>PRO foydalanuvchilar</b>"]

    for row in rows:
        lines.append(
            f"\n<code>{row['user_id']}</code> — {row['expires_at']}"
        )

    await message.answer("\n".join(lines))


# =========================================================
# WEBHOOK
# =========================================================

async def set_webhook_with_retry():
    while True:
        try:
            await bot.set_webhook(
                f"{BASE_URL}/{WEBHOOK_PATH}",
                secret_token=WEBHOOK_SECRET,
                drop_pending_updates=True,
                allowed_updates=dp.resolve_used_update_types(),
                request_timeout=30
            )

            log.info("ASAVED webhook muvaffaqiyatli o‘rnatildi.")
            return

        except asyncio.CancelledError:
            raise

        except Exception:
            log.exception(
                "Telegram bilan aloqa uzildi. 15 soniyadan keyin qayta uriniladi."
            )
            await asyncio.sleep(15)


async def on_startup(app: web.Application):
    init_db()

    app["webhook_task"] = asyncio.create_task(
        set_webhook_with_retry()
    )

    log.info("ASAVED server ishga tushdi.")


async def on_shutdown(app: web.Application):
    task = app.get("webhook_task")

    if task:
        task.cancel()

        with suppress(asyncio.CancelledError):
            await task

    with suppress(Exception):
        await bot.session.close()


# =========================================================
# MAIN
# =========================================================

async def main():
    init_db()

    app = web.Application()

    request_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET
    )

    request_handler.register(
        app,
        path=f"/{WEBHOOK_PATH}"
    )

    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_shutdown)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=port
    )

    await site.start()

    log.info("ASAVED %s-portda ishga tushdi.", port)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
