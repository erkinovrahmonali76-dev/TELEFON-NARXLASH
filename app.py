
import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

import yt_dlp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    KeyboardButton,
    ReplyKeyboardMarkup,
    FSInputFile,
    Update,
)

# SOZLAMALAR
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "asaved_secret_2026")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook")
PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

MAX_SIZE = 45 * 1024 * 1024

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ASAVED")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN Render Environment Variables ichida yo'q")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

user_modes = {}

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="📱 9:16 video"),
            KeyboardButton(text="⭕ Dumaloq video"),
        ],
        [
            KeyboardButton(text="🔗 Video havolasi"),
        ],
    ],
    resize_keyboard=True
)


def valid_video_url(url):
    pattern = (
        r"https?://"
        r"(?:www\.)?"
        r"(?:youtube\.com|youtu\.be|instagram\.com|tiktok\.com)"
        r"(?:/[^\s]*)?"
    )
    return re.fullmatch(pattern, url.strip(), re.I) is not None


def download_media_sync(url, folder):
    folder = Path(folder)

    options = {
        "outtmpl": str(folder / "%(id)s.%(ext)s"),
        "format": "best[height<=1080]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 20,
        "concurrent_fragment_downloads": 4,
        "merge_output_format": "mp4",
        "max_filesize": MAX_SIZE,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    files = [
        p for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")
    ]

    if not files:
        raise RuntimeError("Video topilmadi")

    result = max(files, key=lambda p: p.stat().st_size)

    if result.stat().st_size > MAX_SIZE:
        raise RuntimeError("Video hajmi juda katta")

    return result


async def ffmpeg_convert(source, output, mode):
    if mode == "round":
        video_filter = (
            "scale=640:640:force_original_aspect_ratio=increase,"
            "crop=640:640"
        )
    else:
        video_filter = (
            "scale=540:960:force_original_aspect_ratio=increase,"
            "crop=540:960"
        )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(source),
        "-vf", video_filter,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "27",
        "-c:a", "aac",
        "-b:a", "96k",
        "-threads", "2",
        "-movflags", "+faststart",
        str(output),
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    _, error = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(
            error.decode(errors="ignore")[-700:]
        )

    if not output.exists():
        raise RuntimeError("Tayyor video yaratilmagan")

    if output.stat().st_size > MAX_SIZE:
        raise RuntimeError("Tayyor video hajmi juda katta")

    return output


async def send_converted(message, source, mode):
    status = await message.answer("⏳ Video tayyorlanmoqda...")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "asaved.mp4"

            await ffmpeg_convert(source, output, mode)

            if mode == "round":
                await message.answer_video_note(
                    video=FSInputFile(output)
                )
            else:
                await message.answer_video(
                    video=FSInputFile(output),
                    caption="✅ 9:16 video tayyor!"
                )

        await status.delete()

    except Exception:
        log.exception("VIDEO CONVERSION ERROR")
        await status.edit_text(
            "❌ Videoni tayyorlab bo'lmadi. Boshqa video yuborib ko'ring."
        )


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👋 ASAVED botiga xush kelibsiz!\n\n"
        "📱 9:16 video — vertikal video tayyorlaydi.\n"
        "⭕ Dumaloq video — Telegram dumaloq video qiladi.\n"
        "🔗 Video havolasi — YouTube, Instagram va TikTok videosini yuklaydi.",
        reply_markup=keyboard
    )


@dp.message(F.text == "📱 9:16 video")
async def choose_vertical(message: Message):
    user_modes[message.from_user.id] = "916"
    await message.answer("📹 9:16 qilmoqchi bo'lgan videongizni yuboring.")


@dp.message(F.text == "⭕ Dumaloq video")
async def choose_round(message: Message):
    user_modes[message.from_user.id] = "round"
    await message.answer("⭕ Dumaloq qilmoqchi bo'lgan videongizni yuboring.")


@dp.message(F.text == "🔗 Video havolasi")
async def choose_link(message: Message):
    user_modes[message.from_user.id] = "link"
    await message.answer(
        "🔗 YouTube, Instagram yoki TikTok video havolasini yuboring."
    )


@dp.message(F.video)
async def uploaded_video(message: Message):
    uid = message.from_user.id
    mode = user_modes.get(uid)

    if mode not in ("916", "round"):
        await message.answer(
            "Avval 📱 9:16 video yoki ⭕ Dumaloq video tugmasini bosing.",
            reply_markup=keyboard
        )
        return

    if message.video.file_size and message.video.file_size > MAX_SIZE:
        await message.answer("❌ Video hajmi juda katta.")
        return

    status = await message.answer("⏳ Video olinmoqda...")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"

            file = await bot.get_file(message.video.file_id)
            await bot.download_file(file.file_path, destination=source)

            if source.stat().st_size > MAX_SIZE:
                raise RuntimeError("Video hajmi juda katta")

            await status.delete()
            await send_converted(message, source, mode)

    except Exception:
        log.exception("UPLOAD ERROR")
        await status.edit_text("❌ Videoni olishda xatolik yuz berdi.")

    finally:
        user_modes.pop(uid, None)


@dp.message(F.document)
async def uploaded_document(message: Message):
    doc = message.document

    if not doc.mime_type or not doc.mime_type.startswith("video/"):
        return

    mode = user_modes.get(message.from_user.id)

    if mode not in ("916", "round"):
        await message.answer("Avval video formatini tanlang.")
        return

    if doc.file_size and doc.file_size > MAX_SIZE:
        await message.answer("❌ Video hajmi juda katta.")
        return

    status = await message.answer("⏳ Video olinmoqda...")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"

            file = await bot.get_file(doc.file_id)
            await bot.download_file(file.file_path, destination=source)

            await status.delete()
            await send_converted(message, source, mode)

    except Exception:
        log.exception("DOCUMENT ERROR")
        await status.edit_text("❌ Videoni olishda xatolik yuz berdi.")

    finally:
        user_modes.pop(message.from_user.id, None)


@dp.message(F.text)
async def link_handler(message: Message):
    url = (message.text or "").strip()

    if not valid_video_url(url):
        return

    status = await message.answer("⏳ Video yuklanmoqda...")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            source = await asyncio.to_thread(
                download_media_sync, url, tmp
            )

            output = Path(tmp) / "vertical.mp4"

            await ffmpeg_convert(source, output, "916")

            await message.answer_video(
                video=FSInputFile(output),
                caption="✅ 9:16 video tayyor!"
            )

        await status.delete()

    except Exception:
        log.exception("LINK DOWNLOAD ERROR")
        await status.edit_text(
            "❌ Video yuklanmadi.\n"
            "Havola ochiq ekanini tekshiring yoki boshqa havola yuboring."
        )


async def health(request):
    return web.Response(text="ASAVED OK")


async def webhook(request):
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != WEBHOOK_SECRET:
            return web.Response(status=403)

    try:
        data = await request.json()
        update = Update.model_validate(data, context={"bot": bot})
        await dp.feed_update(bot, update)
        return web.Response(text="OK")
    except Exception:
        log.exception("WEBHOOK ERROR")
        return web.Response(status=500)


async def on_startup(app):
    if not BASE_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL topilmadi")

    await bot.set_webhook(
        url=f"{BASE_URL}/{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET
    )
    log.info("ASAVED webhook ishga tushdi")


async def on_shutdown(app):
    await bot.session.close()


app = web.Application()
app.router.add_get("/", health)
app.router.add_post(f"/{WEBHOOK_PATH}", webhook)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)


if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
