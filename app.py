
import os
import re
import html
import asyncio
import logging
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import yt_dlp

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, ReplyKeyboardMarkup,
    KeyboardButton, FSInputFile, Update
)

# ==========================================
# SOZLAMALAR
# ==========================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv(
    "WEBHOOK_PATH", "asaved-webhook"
).strip("/")
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID", "").strip()

PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

MAX_SIZE = 45 * 1024 * 1024
TIMEOUT = 240

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("ASAVED")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN Environment'da yo'q")

if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
    raise RuntimeError("WEBHOOK_SECRET kamida 16 belgi bo'lishi kerak")

if not re.fullmatch(r"[A-Za-z0-9_-]+", WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET faqat harf, raqam, _ va - dan iborat bo'lsin")

if not BASE_URL.startswith("https://"):
    raise RuntimeError("RENDER_EXTERNAL_URL HTTPS manzil bo'lishi kerak")

if not WEBHOOK_PATH or "/" in WEBHOOK_PATH:
    raise RuntimeError("WEBHOOK_PATH noto'g'ri")

# ==========================================
# BOT
# ==========================================

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()

# Har bir foydalanuvchi va chat uchun alohida ma'lumot
user_songs = {}
user_video_urls = {}


def user_key(user_id, chat_id):
    return (user_id, chat_id)


# ==========================================
# MENYU
# ==========================================

menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎵 Musiqa qidirish")],
        [KeyboardButton(text="📥 Video yuklash")],
        [KeyboardButton(text="ℹ️ Yordam")]
    ],
    resize_keyboard=True
)


def video_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📥 Saqlash",
                    callback_data="save_video"
                ),
                InlineKeyboardButton(
                    text="🎵 MP3 olish",
                    callback_data="video_mp3"
                )
            ]
        ]
    )


# ==========================================
# HAVOLA TEKSHIRISH
# ==========================================

def is_video_url(text):
    try:
        parsed = urlparse(text.strip())
        host = (parsed.hostname or "").lower()

        allowed = (
            "youtube.com",
            "youtu.be",
            "youtube-nocookie.com",
            "instagram.com",
            "instagr.am",
            "tiktok.com"
        )

        valid_host = any(
            host == domain or host.endswith("." + domain)
            for domain in allowed
        )

        return (
            parsed.scheme == "https"
            and valid_host
        )

    except Exception:
        return False


# ==========================================
# JAMENDO MUSIQA QIDIRISH
# ==========================================

async def jamendo_search(query):
    if not JAMENDO_CLIENT_ID:
        raise RuntimeError("JAMENDO_CLIENT_ID kiritilmagan")

    api_url = "https://api.jamendo.com/v3.0/tracks/"

    params = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        "limit": 10,
        "search": query,
        "order": "relevance",
        "include": "musicinfo"
    }

    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            api_url,
            params=params
        ) as response:

            if response.status != 200:
                body = await response.text()
                logger.error("Jamendo API: %s", body[:500])
                raise RuntimeError("Jamendo API xatosi")

            data = await response.json()

    if data.get("headers", {}).get("status") != "success":
        raise RuntimeError("Jamendo qidiruv xatosi")

    return data.get("results", [])


def song_keyboard(results):
    rows = []

    for start in range(0, min(len(results), 10), 5):
        row = []

        for index in range(start, min(start + 5, len(results))):
            row.append(
                InlineKeyboardButton(
                    text=str(index + 1),
                    callback_data=f"song:{index}"
                )
            )

        rows.append(row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==========================================
# START
# ==========================================

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🎧 <b>ASAVED BOT</b>\n\n"
        "Assalomu alaykum!\n\n"
        "🎵 Qo'shiq nomi yoki ijrochi ismini yuboring.\n"
        "🔟 Sizga 10 ta musiqa natijasini chiqaraman.\n\n"
        "📥 YouTube, Instagram yoki TikTok havolasini yuboring.\n"
        "🎬 Ruxsatingiz bo'lgan videolarni yuklashga yordam beraman.",
        reply_markup=menu
    )


@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Yordam")
async def help_message(message: Message):
    await message.answer(
        "ℹ️ <b>ASAVED BOT YORDAM</b>\n\n"
        "🎵 Musiqa qidirish — qo'shiq yoki ijrochi nomini yozing.\n"
        "📥 Video yuklash — video havolasini yuboring.\n"
        "🎧 MP3 olish — yuborilgan videodagi MP3 tugmasini bosing.\n\n"
        "Faqat o'zingizga tegishli yoki yuklashga ruxsat berilgan "
        "kontentdan foydalaning."
    )


@dp.message(F.text == "🎵 Musiqa qidirish")
async def ask_music(message: Message):
    await message.answer(
        "🎵 Qo'shiq nomi yoki ijrochi ismini yuboring:"
    )


@dp.message(F.text == "📥 Video yuklash")
async def ask_video(message: Message):
    await message.answer(
        "📎 Video havolasini yuboring:"
    )


# ==========================================
# UMUMIY MATN HANDLER
# ==========================================

@dp.message(F.text)
async def text_handler(message: Message):
    text = message.text.strip()

    if text.startswith("/"):
        return

    if is_video_url(text):
        await process_video(message, text)
        return

    status = await message.answer(
        "🔎 Musiqa qidirilmoqda..."
    )

    try:
        results = await jamendo_search(text)

        if not results:
            await status.edit_text(
                "😕 Hech qanday musiqa topilmadi."
            )
            return

        key = user_key(
            message.from_user.id,
            message.chat.id
        )

        user_songs[key] = results

        lines = ["🎵 <b>TOPILGAN MUSIQALAR</b>\n"]

        for i, song in enumerate(results[:10], 1):
            title = html.escape(
                song.get("name") or "Noma'lum"
            )
            artist = html.escape(
                song.get("artist_name") or "Noma'lum"
            )

            duration = int(song.get("duration") or 0)
            minutes, seconds = divmod(duration, 60)

            lines.append(
                f"<b>{i}.</b> {title}\n"
                f"👤 {artist} | ⏱ {minutes}:{seconds:02d}\n"
            )

        lines.append("👇 Kerakli raqamni tanlang:")

        await status.edit_text(
            "\n".join(lines),
            reply_markup=song_keyboard(results)
        )

    except Exception:
        logger.exception("MUSIC SEARCH ERROR")

        await status.edit_text(
            "❌ Musiqa qidirishda xatolik.\n"
            "Jamendo Client ID va API holatini tekshiring."
        )


# ==========================================
# VIDEO YUKLASH
# ==========================================

def download_youtube(url, options):
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])


async def process_video(message: Message, url: str):
    status = await message.answer(
        "⏳ Video tayyorlanmoqda..."
    )

    try:
        with tempfile.TemporaryDirectory() as folder:
            output = str(
                Path(folder) / "%(id)s.%(ext)s"
            )

            options = {
                "format": (
                    "best[ext=mp4][vcodec!=none][acodec!=none]"
                    "/best[ext=mp4]/best"
                ),
                "outtmpl": output,
                "merge_output_format": "mp4",
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 30,
                "retries": 2,
                "max_filesize": MAX_SIZE,
            }

            await asyncio.wait_for(
                asyncio.to_thread(
                    download_youtube,
                    url,
                    options
                ),
                timeout=TIMEOUT
            )

            files = [
                p for p in Path(folder).iterdir()
                if p.is_file()
                and p.suffix.lower() not in (".part", ".ytdl")
            ]

            if not files:
                raise RuntimeError("Video fayli topilmadi")

            filepath = max(
                files,
                key=lambda p: p.stat().st_size
            )

            if filepath.stat().st_size > MAX_SIZE:
                raise RuntimeError("Video 45 MB dan katta")

            key = user_key(
                message.from_user.id,
                message.chat.id
            )

            user_video_urls[key] = url

            # Fayl vaqtinchalik papka ichida turgan paytda yuboriladi
            await message.answer_video(
                FSInputFile(filepath),
                caption="🎬 <b>ASAVED BOT</b>",
                reply_markup=video_keyboard(),
                supports_streaming=True
            )

            await status.delete()

    except asyncio.TimeoutError:
        await status.edit_text(
            "⌛ Yuklash vaqti tugadi. Keyinroq urinib ko'ring."
        )

    except Exception:
        logger.exception("VIDEO DOWNLOAD ERROR")

        await status.edit_text(
            "❌ Video yuklanmadi.\n"
            "Havolani tekshiring yoki keyinroq urinib ko'ring."
        )


# ==========================================
# VIDEO DAN MP3
# ==========================================

async def download_audio(url, folder):
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(
            Path(folder) / "%(id)s.%(ext)s"
        ),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_SIZE,
        "socket_timeout": 30,
        "retries": 2,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192"
            }
        ]
    }

    await asyncio.wait_for(
        asyncio.to_thread(
            download_youtube,
            url,
            options
        ),
        timeout=TIMEOUT
    )

    files = list(Path(folder).glob("*.mp3"))

    if not files:
        raise RuntimeError("MP3 fayli topilmadi")

    if files[0].stat().st_size > MAX_SIZE:
        raise RuntimeError("MP3 fayli juda katta")

    return files[0]


# ==========================================
# MUSIQA TANLASH
# ==========================================

@dp.callback_query(F.data.startswith("song:"))
async def select_song(callback: CallbackQuery):
    try:
        index = int(callback.data.split(":")[1])

        key = user_key(
            callback.from_user.id,
            callback.message.chat.id
        )

        songs = user_songs.get(key, [])

        if index < 0 or index >= len(songs):
            await callback.answer(
                "Qidiruvni qayta bajaring.",
                show_alert=True
            )
            return

        song = songs[index]

        title = html.escape(
            song.get("name") or "Noma'lum"
        )
        artist = html.escape(
            song.get("artist_name") or "Noma'lum"
        )

        share_url = song.get("shareurl")

        await callback.answer("Musiqa topildi!")

        buttons = []

        if share_url:
            buttons.append([
                InlineKeyboardButton(
                    text="🎵 Jamendo'da tinglash",
                    url=share_url
                )
            ])

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=buttons
        ) if buttons else None

        await callback.message.answer(
            f"🎵 <b>{title}</b>\n"
            f"👤 {artist}\n\n"
            "Bu trekning litsenziyasi va foydalanish "
            "shartlarini Jamendo sahifasida tekshiring.",
            reply_markup=keyboard
        )

    except Exception:
        logger.exception("SONG CALLBACK ERROR")
        await callback.answer(
            "Musiqa ma'lumotini olishda xatolik.",
            show_alert=True
        )


# ==========================================
# VIDEO TUGMALARI
# ==========================================

@dp.callback_query(F.data == "save_video")
async def save_video(callback: CallbackQuery):
    await callback.answer(
        "Videoni Telegramdagi saqlash tugmasi orqali saqlang.",
        show_alert=True
    )


@dp.callback_query(F.data == "video_mp3")
async def video_mp3(callback: CallbackQuery):
    key = user_key(
        callback.from_user.id,
        callback.message.chat.id
    )

    url = user_video_urls.get(key)

    if not url:
        await callback.answer(
            "Avval video havolasini yuboring.",
            show_alert=True
        )
        return

    await callback.answer("Audio tayyorlanmoqda...")

    status = await callback.message.answer(
        "🎧 Audio tayyorlanmoqda..."
    )

    try:
        with tempfile.TemporaryDirectory() as folder:
            path = await download_audio(url, folder)

            await callback.message.answer_audio(
                FSInputFile(path),
                caption="🎵 <b>ASAVED BOT</b>"
            )

        await status.delete()

    except Exception:
        logger.exception("AUDIO EXTRACTION ERROR")

        await status.edit_text(
            "❌ Audio tayyorlanmadi.\n"
            "Havola yoki video formatini tekshiring."
        )


# ==========================================
# WEBHOOK VA HEALTH CHECK
# ==========================================

async def health(request):
    return web.json_response({
        "status": "ok",
        "bot": "ASAVED"
    })


async def telegram_webhook(request):
    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if received_secret != WEBHOOK_SECRET:
        logger.warning("Webhook secret mos kelmadi")
        raise web.HTTPUnauthorized()

    try:
        data = await request.json()

        telegram_update = Update.model_validate(
            data,
            context={"bot": bot}
        )

        logger.info(
            "Telegram update keldi: %s",
            telegram_update.update_id
        )

        await dp.feed_update(
            bot,
            telegram_update
        )

        return web.Response(text="OK")

    except Exception:
        logger.exception("WEBHOOK PROCESSING ERROR")
        raise web.HTTPInternalServerError()


# ==========================================
# STARTUP
# ==========================================

async def on_startup(app):
    me = await bot.get_me()

    webhook_url = (
        f"{BASE_URL}/{WEBHOOK_PATH}"
    )

    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types()
    )

    info = await bot.get_webhook_info()

    logger.info("ASAVED BOT STARTED: @%s", me.username)
    logger.info("WEBHOOK URL: %s", info.url)
    logger.info(
        "WEBHOOK PENDING: %s",
        info.pending_update_count
    )
    logger.info(
        "WEBHOOK ERROR: %s",
        info.last_error_message
    )


async def on_shutdown(app):
    # Webhookni o'chirmaymiz!
    await bot.session.close()


# ==========================================
# SERVER
# ==========================================

app = web.Application(
    client_max_size=2 * 1024 * 1024
)

app.router.add_get("/", health)
app.router.add_get("/health", health)

app.router.add_post(
    f"/{WEBHOOK_PATH}",
    telegram_webhook
)

app.on_startup.append(on_startup)
app.on_cleanup.append(on_shutdown)


if __name__ == "__main__":
    web.run_app(
        app,
        host="0.0.0.0",
        port=PORT
    )
