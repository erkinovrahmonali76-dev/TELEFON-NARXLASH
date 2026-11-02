import os
import re
import glob
import asyncio
import logging
import tempfile
import subprocess
from urllib.parse import urlparse

import aiohttp
import yt_dlp

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, Update, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode


# ==================================
# SETTINGS
# ==================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", "10000"))

MAX_FILE_SIZE = 49 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s"
)
logger = logging.getLogger("ASAVED")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN kiritilmagan")

if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
    raise RuntimeError("WEBHOOK_SECRET kamida 16 belgi bo‘lsin")

if not re.fullmatch(r"[A-Za-z0-9_-]+", WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET noto‘g‘ri formatda")

if not RENDER_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL topilmadi")


bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# Qidiruv natijalari
search_cache = {}

# Tasdiqlangan video havolalari
pending_videos = {}


# ==================================
# START
# ==================================

@dp.message(CommandStart())
async def start(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🎵 Musiqa qidirish",
                callback_data="music_help"
            )
        ],
        [
            InlineKeyboardButton(
                text="🎬 Video yuklash",
                callback_data="video_help"
            )
        ],
        [
            InlineKeyboardButton(
                text="ℹ️ Yordam",
                callback_data="help"
            )
        ]
    ])

    await message.answer(
        "🎧 <b>ASAVED BOT</b>\n\n"
        "Musiqa qidiring va ruxsatli videolaringizni "
        "9:16 formatga aylantiring.\n\n"
        "🎵 Qo‘shiq nomini yozing.\n"
        "🎬 Video havolasini yuboring.",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "music_help")
async def music_help(call: CallbackQuery):
    await call.message.answer(
        "🎵 Musiqa nomini yoki ijrochi nomini yuboring.\n"
        "Masalan: Alan Walker"
    )
    await call.answer()


@dp.callback_query(F.data == "video_help")
async def video_help(call: CallbackQuery):
    await call.message.answer(
        "🎬 Instagram, TikTok yoki YouTube video havolasini yuboring.\n\n"
        "Faqat o‘zingizga tegishli yoki yuklashga "
        "ruxsatingiz bor videolarni yuboring."
    )
    await call.answer()


@dp.callback_query(F.data == "help")
async def help_callback(call: CallbackQuery):
    await call.message.answer(
        "🎧 <b>ASAVED YORDAM</b>\n\n"
        "• Musiqa nomi — qidiruv\n"
        "• Video havolasi — 9:16 format\n"
        "• /start — bosh menyu\n"
        "• /help — yordam"
    )
    await call.answer()


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "🎵 Musiqa qidirish uchun nom yuboring.\n"
        "🎬 Video uchun havolani yuboring.\n"
        "📐 Video 9:16 formatga aylantiriladi."
    )


# ==================================
# JAMENDO MUSIC SEARCH
# ==================================

async def jamendo_search(query):
    if not JAMENDO_CLIENT_ID:
        raise RuntimeError("JAMENDO_CLIENT_ID kiritilmagan")

    url = "https://api.jamendo.com/v3.0/tracks/"

    params = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        "limit": 10,
        "search": query,
        "type": "single albumtrack",
        "include": "musicinfo",
        "order": "relevance"
    }

    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as response:
            data = await response.json(content_type=None)

            logger.info(
                "JAMENDO STATUS=%s HEADERS=%s",
                response.status,
                data.get("headers")
            )

            if response.status != 200:
                raise RuntimeError("Jamendo HTTP xatosi")

            headers = data.get("headers", {})

            if headers.get("status") != "success":
                raise RuntimeError(
                    str(headers.get("error_message"))
                )

            results = data.get("results", [])
            logger.info("JAMENDO RESULTS=%s", len(results))

            return results


# ==================================
# MUSIC KEYBOARD
# ==================================

def music_keyboard():
    rows = []

    for start in (0, 5):
        rows.append([
            InlineKeyboardButton(
                text=str(i),
                callback_data=f"track:{i - 1}"
            )
            for i in range(start + 1, start + 6)
        ])

    rows.append([
        InlineKeyboardButton(
            text="⬅️",
            callback_data="music_prev"
        ),
        InlineKeyboardButton(
            text="❌",
            callback_data="music_close"
        ),
        InlineKeyboardButton(
            text="➡️",
            callback_data="music_next"
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def music_text(results, query, page):
    start = page * 10
    selected = results[start:start + 10]

    lines = [
        f"🎵 <b>{query}</b>",
        f"Natijalar: {len(results)} ta",
        ""
    ]

    for i, track in enumerate(selected, start=1):
        duration = int(track.get("duration") or 0)
        artist = track.get("artist_name", "Noma'lum")
        name = track.get("name", "Noma'lum")

        lines.append(
            f"<b>{i}.</b> {artist} - {name} "
            f"({duration // 60}:{duration % 60:02d})"
        )

    lines.append("\n👇 Qo‘shiq raqamini tanlang.")

    return "\n".join(lines)


async def show_music(message, query):
    loading = await message.answer("🔍 Musiqa qidirilmoqda...")

    try:
        results = await jamendo_search(query)

        if not results:
            await loading.edit_text(
                "😕 Hech qanday natija topilmadi.\n"
                "Boshqa nom bilan qidirib ko‘ring."
            )
            return

        chat_id = message.chat.id

        search_cache[chat_id] = {
            "query": query,
            "results": results,
            "page": 0
        }

        await loading.edit_text(
            music_text(results, query, 0),
            reply_markup=music_keyboard()
        )

    except Exception:
        logger.exception("Music search error")
        await loading.edit_text(
            "⚠️ Musiqa qidirishda xatolik yuz berdi."
        )


# ==================================
# MUSIC PAGE
# ==================================

@dp.callback_query(F.data.in_({"music_prev", "music_next"}))
async def music_page(call: CallbackQuery):
    chat_id = call.message.chat.id
    data = search_cache.get(chat_id)

    if not data:
        await call.answer("Qidiruv muddati tugadi.", show_alert=True)
        return

    max_page = (len(data["results"]) - 1) // 10

    if call.data == "music_next":
        data["page"] = min(data["page"] + 1, max_page)
    else:
        data["page"] = max(data["page"] - 1, 0)

    await call.message.edit_text(
        music_text(data["results"], data["query"], data["page"]),
        reply_markup=music_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == "music_close")
async def music_close(call: CallbackQuery):
    search_cache.pop(call.message.chat.id, None)
    await call.message.edit_text("❌ Qidiruv yopildi.")
    await call.answer()


# ==================================
# DOWNLOAD AUTHORIZED JAMENDO AUDIO
# ==================================

@dp.callback_query(F.data.startswith("track:"))
async def select_track(call: CallbackQuery):
    try:
        index = int(call.data.split(":")[1])
        data = search_cache.get(call.message.chat.id)

        if not data:
            await call.answer("Qidiruv muddati tugadi.", show_alert=True)
            return

        start = data["page"] * 10
        tracks = data["results"][start:start + 10]

        if index < 0 or index >= len(tracks):
            await call.answer("Noto‘g‘ri raqam.", show_alert=True)
            return

        track = tracks[index]
        name = track.get("name", "Musiqa")
        artist = track.get("artist_name", "Noma'lum")
        share_url = track.get("shareurl")

        allowed = track.get("audiodownload_allowed")
        allowed = str(allowed).lower() in ("true", "1", "yes")

        download_url = track.get("audiodownload")

        if not allowed or not download_url:
            text = (
                f"🎵 <b>{name}</b>\n"
                f"👤 {artist}\n\n"
                "Bu trek uchun MP3 yuklab olishga ruxsat yo‘q."
            )

            keyboard = None

            if share_url:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="🎧 Jamendo'da tinglash",
                        url=share_url
                    )]
                ])

            await call.message.answer(text, reply_markup=keyboard)
            await call.answer()
            return

        await call.answer("Audio tayyorlanmoqda...")

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        ) as session:
            async with session.get(download_url) as response:
                if response.status != 200:
                    raise RuntimeError("Audio yuklab olinmadi")

                if (
                    response.content_length
                    and response.content_length > MAX_FILE_SIZE
                ):
                    raise RuntimeError("Audio fayl juda katta")

                content = await response.read()

        if len(content) > MAX_FILE_SIZE:
            raise RuntimeError("Audio fayl Telegram limitidan katta")

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "music.mp3")

            with open(path, "wb") as file:
                file.write(content)

            await bot.send_audio(
                chat_id=call.message.chat.id,
                audio=FSInputFile(path, filename="ASAVED.mp3"),
                title=name,
                performer=artist,
                caption=(
                    f"🎵 <b>{name}</b>\n"
                    f"👤 {artist}\n\n"
                    "🎧 ASAVED MUSIC"
                )
            )

    except Exception:
        logger.exception("Audio download error")
        await call.message.answer(
            "⚠️ Audio yuborilmadi. Keyinroq urinib ko‘ring."
        )


# ==================================
# VIDEO URL CHECK
# ==================================

ALLOWED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
)


def valid_video_url(url):
    try:
        host = (urlparse(url).hostname or "").lower()

        return any(
            host == domain or host.endswith("." + domain)
            for domain in ALLOWED_DOMAINS
        )
    except Exception:
        return False


@dp.message(Command("video"))
async def video_command(message: Message):
    parts = message.text.split(maxsplit=1)

    if len(parts) < 2 or not valid_video_url(parts[1].strip()):
        await message.answer(
            "🎬 To‘g‘ri video havolasini yuboring.\n"
            "Masalan: /video https://www.youtube.com/watch?v=..."
        )
        return

    await ask_video_permission(message, parts[1].strip())


async def ask_video_permission(message, url):
    key = f"{message.chat.id}:{message.from_user.id}"
    pending_videos[key] = url

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Ruxsatim bor — davom etish",
                callback_data="video_confirm"
            )
        ],
        [
            InlineKeyboardButton(
                text="❌ Bekor qilish",
                callback_data="video_cancel"
            )
        ]
    ])

    await message.answer(
        "🎬 Video havolasi qabul qilindi.\n\n"
        "Faqat o‘zingizga tegishli yoki egasidan "
        "yuklashga ruxsat olgan videoni davom ettiring.",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "video_cancel")
async def video_cancel(call: CallbackQuery):
    key = f"{call.message.chat.id}:{call.from_user.id}"
    pending_videos.pop(key, None)

    await call.message.edit_text("❌ Video yuklash bekor qilindi.")
    await call.answer()


# ==================================
# VIDEO DOWNLOAD
# ==================================

def download_video(url, folder):
    output = os.path.join(folder, "source.%(ext)s")

    options = {
        "outtmpl": output,
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "socket_timeout": 30,
        "max_filesize": MAX_FILE_SIZE * 3,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    files = glob.glob(os.path.join(folder, "source.*"))

    if not files:
        raise RuntimeError("Video fayli topilmadi")

    return max(files, key=os.path.getsize)


def convert_vertical(source, destination):
    command = [
        "ffmpeg",
        "-y",
        "-i", source,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-vf",
        (
            "scale=1080:1920:"
            "force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
            "setsar=1"
        ),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "27",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-t", "600",
        destination
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=900
    )

    if result.returncode != 0:
        logger.error("FFMPEG ERROR: %s", result.stderr[-2000:])
        raise RuntimeError("Video formatga aylantirilmadi")


async def process_video(message, url):
    status = await message.answer(
        "⏳ Video yuklanmoqda...\n"
        "Iltimos, kuting."
    )

    try:
        with tempfile.TemporaryDirectory() as folder:
            source = await asyncio.to_thread(
                download_video,
                url,
                folder
            )

            await status.edit_text(
                "🎬 Video 9:16 formatga o‘tkazilmoqda..."
            )

            destination = os.path.join(folder, "ASAVED_9x16.mp4")

            await asyncio.to_thread(
                convert_vertical,
                source,
                destination
            )

            size = os.path.getsize(destination)

            if size > MAX_FILE_SIZE:
                await status.edit_text(
                    "⚠️ Tayyor video Telegram yuborish limitidan katta."
                )
                return

            await status.edit_text("📤 Telegramga yuborilmoqda...")

            await message.answer_video(
                video=FSInputFile(destination),
                caption=(
                    "🎬 <b>ASAVED VIDEO</b>\n"
                    "📐 Format: 9:16\n"
                    "📱 O‘lcham: 1080 × 1920"
                ),
                supports_streaming=True
            )

            await status.delete()

    except Exception:
        logger.exception("Video processing error")
        await status.edit_text(
            "⚠️ Video yuklashda xatolik.\n\n"
            "Havola ochiq ekanini tekshiring yoki "
            "boshqa video yuboring."
        )


@dp.callback_query(F.data == "video_confirm")
async def video_confirm(call: CallbackQuery):
    key = f"{call.message.chat.id}:{call.from_user.id}"
    url = pending_videos.pop(key, None)

    if not url:
        await call.answer(
            "Havola muddati tugadi. Qayta yuboring.",
            show_alert=True
        )
        return

    await call.answer("Boshlanmoqda...")

    await call.message.edit_text("⏳ Video tayyorlanmoqda...")

    await process_video(call.message, url)


# ==================================
# TEXT HANDLER
# ==================================

@dp.message(F.text)
async def text_handler(message: Message):
    text = message.text.strip()

    if valid_video_url(text):
        await ask_video_permission(message, text)
        return

    if text.startswith("/"):
        return

    await show_music(message, text)


# ==================================
# WEBHOOK
# ==================================

async def health(request):
    return web.json_response({
        "status": "ok",
        "bot": "ASAVED",
        "version": "2.0"
    })


async def telegram_webhook(request):
    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if secret != WEBHOOK_SECRET:
        return web.Response(status=403)

    try:
        data = await request.json()

        update = Update.model_validate(
            data,
            context={"bot": bot}
        )

        await dp.feed_update(bot, update)

        return web.Response(text="OK")

    except Exception:
        logger.exception("WEBHOOK ERROR")
        return web.Response(status=500)


# ==================================
# MAIN
# ==================================

async def main():
    app = web.Application()

    app.router.add_get("/", health)

    app.router.add_post(
        f"/{WEBHOOK_PATH}",
        telegram_webhook
    )

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    webhook_url = (
        f"{RENDER_URL.rstrip('/')}/{WEBHOOK_PATH}"
    )

    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True
    )

    logger.info("ASAVED BOT IS LIVE")
    logger.info("WEBHOOK: %s", webhook_url)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
