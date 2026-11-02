
import os
import re
import asyncio
import logging
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from contextlib import closing

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, Update, InlineKeyboardMarkup,
    InlineKeyboardButton, FSInputFile
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ASAVED")

# ENVIRONMENT VARIABLES
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-hook").strip("/")
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
PORT = int(os.getenv("PORT", "10000"))

DB_PATH = "asaved.sqlite3"
MAX_SIZE = 45 * 1024 * 1024
TIMEOUT = 240

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "youtu.be", "music.youtube.com",
    "tiktok.com", "www.tiktok.com",
    "vm.tiktok.com", "vt.tiktok.com",
    "instagram.com", "www.instagram.com",
    "instagr.am", "www.instagr.am"
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN kiritilmagan")

if len(WEBHOOK_SECRET) < 16:
    raise RuntimeError("WEBHOOK_SECRET kamida 16 belgi bo‘lsin")

if not PUBLIC_URL:
    raise RuntimeError("Render URL topilmadi")

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()
download_lock = asyncio.Semaphore(2)


# DATABASE
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                url TEXT,
                title TEXT
            )
        """)
        con.commit()


def save_job(user_id, url, title):
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.execute(
            "INSERT INTO jobs(user_id,url,title) VALUES(?,?,?)",
            (user_id, url, title)
        )
        con.commit()
        return cur.lastrowid


def get_job(job_id, user_id):
    with closing(sqlite3.connect(DB_PATH)) as con:
        return con.execute(
            "SELECT url,title FROM jobs WHERE id=? AND user_id=?",
            (job_id, user_id)
        ).fetchone()


# URL CHECK
def valid_url(url):
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()

        return (
            parsed.scheme in ("http", "https")
            and host in ALLOWED_HOSTS
        )
    except Exception:
        return False


# DOWNLOAD
def download_media(url, folder, audio=False):

    output = str(Path(folder) / "%(title).70s-%(id)s.%(ext)s")

    options = {
        "outtmpl": output,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 25,
        "retries": 2,
        "max_filesize": MAX_SIZE,
        "restrictfilenames": True,
    }

    if audio:
        options.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        })
    else:
        options.update({
            "format": "best[ext=mp4]/best",
            "merge_output_format": "mp4",
        })

    with YoutubeDL(options) as ydl:

        info = ydl.extract_info(url, download=True)
        title = info.get("title") or "ASAVED Media"

        if audio:
            filename = Path(ydl.prepare_filename(info)).with_suffix(".mp3")
        else:
            filename = Path(ydl.prepare_filename(info))

        if not filename.exists():
            files = [
                p for p in Path(folder).iterdir()
                if p.is_file()
                and p.suffix.lower() in (
                    [".mp3"] if audio else [".mp4", ".mkv", ".webm"]
                )
            ]

            if not files:
                raise RuntimeError("Yuklangan fayl topilmadi")

            filename = files[0]

        if filename.stat().st_size > MAX_SIZE:
            raise RuntimeError("Fayl 45 MB dan katta")

        return str(filename), title


# START
@dp.message(CommandStart())
async def start(message: Message):

    await message.answer(
        "🎧 <b>ASAVED LINK BOT</b>\n\n"
        "YouTube, TikTok yoki Instagram havolasini yuboring.\n\n"
        "🎬 Video Telegram pleerida chiqadi.\n"
        "🎵 MP3 qilib olish mumkin.\n"
        "💾 Videoni Telegram orqali saqlash mumkin.\n\n"
        "Faqat yuklab olishga ruxsat berilgan videolardan foydalaning."
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "📌 <b>Foydalanish:</b>\n\n"
        "1. YouTube video havolasini yuboring.\n"
        "2. TikTok havolasini yuboring.\n"
        "3. Instagram Reels havolasini yuboring.\n\n"
        "Bot videoni tayyorlab Telegram'ga yuboradi."
    )


# VIDEO DOWNLOAD
@dp.message(F.text)
async def video_handler(message: Message):

    url = message.text.strip()

    if not valid_url(url):
        return await message.answer(
            "❌ YouTube, TikTok yoki Instagram havolasini yuboring."
        )

    status = await message.answer(
        "⏳ <b>Video yuklanmoqda...</b>\n"
        "Iltimos, kuting."
    )

    async with download_lock:

        with tempfile.TemporaryDirectory() as folder:

            try:
                filepath, title = await asyncio.wait_for(
                    asyncio.to_thread(
                        download_media, url, folder, False
                    ),
                    timeout=TIMEOUT
                )

                job_id = save_job(
                    message.from_user.id,
                    url,
                    title
                )

                username = (await bot.get_me()).username or "ASAVED"

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="💾 Saqlash",
                                callback_data="save"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🎵 Qo‘shiqni MP3 qilib olish",
                                callback_data=f"mp3:{job_id}"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="Guruhga qo‘shish ↗️",
                                url=f"https://t.me/{username}?startgroup=true"
                            )
                        ]
                    ]
                )

                await message.answer_video(
                    video=FSInputFile(filepath),
                    caption=(
                        f"📥 <b>@{username}</b> orqali yuklab olindi"
                    ),
                    supports_streaming=True,
                    reply_markup=keyboard
                )

                await status.delete()

            except asyncio.TimeoutError:
                await status.edit_text(
                    "⌛ Yuklash vaqti tugadi. Keyinroq urinib ko‘ring."
                )

            except DownloadError:
                log.exception("Download error")
                await status.edit_text(
                    "❌ Video olinmadi. Havola ochiq ekanini tekshiring."
                )

            except Exception:
                log.exception("Video error")
                await status.edit_text(
                    "❌ Xatolik yuz berdi.\n"
                    "Video katta, yopiq yoki sayt tomonidan qo‘llanmayotgan bo‘lishi mumkin."
                )


# SAVE BUTTON
@dp.callback_query(F.data == "save")
async def save_button(call: CallbackQuery):

    await call.answer(
        "Videoni telefoningizga saqlash uchun video ichidagi yuklab olish belgisini bosing.",
        show_alert=True
    )


# MP3 BUTTON
@dp.callback_query(F.data.startswith("mp3:"))
async def mp3_button(call: CallbackQuery):

    try:
        job_id = int(call.data.split(":")[1])
    except Exception:
        return await call.answer("Noto‘g‘ri so‘rov", show_alert=True)

    job = get_job(job_id, call.from_user.id)

    if not job:
        return await call.answer(
            "Havola topilmadi. Qayta yuboring.",
            show_alert=True
        )

    url, title = job

    await call.answer("MP3 tayyorlanmoqda")

    status = await call.message.answer(
        "🎵 <b>MP3 tayyorlanmoqda...</b>"
    )

    async with download_lock:

        with tempfile.TemporaryDirectory() as folder:

            try:
                filepath, audio_title = await asyncio.wait_for(
                    asyncio.to_thread(
                        download_media, url, folder, True
                    ),
                    timeout=TIMEOUT
                )

                await call.message.answer_audio(
                    audio=FSInputFile(filepath),
                    title=audio_title[:64],
                    caption=f"🎵 <b>{audio_title}</b>"
                )

                await status.delete()

            except Exception:
                log.exception("MP3 error")
                await status.edit_text(
                    "❌ MP3 tayyorlab bo‘lmadi."
                )


# HEALTH CHECK
async def health(request):

    return web.json_response({
        "status": "ok",
        "bot": "ASAVED"
    })


# WEBHOOK
async def webhook(request):

    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if secret != WEBHOOK_SECRET:
        raise web.HTTPUnauthorized()

    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)

    except Exception:
        log.exception("Webhook update error")

    return web.Response(text="OK")


# SERVER
async def main():

    init_db()

    webhook_url = (
        f"{PUBLIC_URL.rstrip('/')}/{WEBHOOK_PATH}"
    )

    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types()
    )

    app = web.Application()

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post(
        f"/{WEBHOOK_PATH}",
        webhook
    )

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    logging.info("ASAVED BOT IS RUNNING")

    try:
        await asyncio.Event().wait()

    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
