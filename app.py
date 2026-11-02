
import asyncio
import logging
import os
import re
import shutil
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

# ==============================
# ASAVED SOZLAMALARI
# ==============================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "asaved_secret_2026")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook")
PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

MAX_SIZE = 45 * 1024 * 1024
DOWNLOAD_LIMIT = asyncio.Semaphore(2)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("ASAVED")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN kiritilmagan!")

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


# ==============================
# HAVOLANI TEKSHIRISH
# ==============================

def valid_video_url(url):
    pattern = (
        r"https?://"
        r"(?:www\.|m\.)?"
        r"(?:youtube\.com|youtu\.be|instagram\.com|"
        r"tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)"
        r"(?:/[^\s]*)?"
    )

    return re.fullmatch(pattern, url.strip(), re.I) is not None


# ==============================
# VIDEO YUKLASH
# ==============================

def download_media_sync(url, folder):
    folder = Path(folder)

    options = {
        "outtmpl": str(folder / "%(id)s.%(ext)s"),
        "format": "best[height<=1080]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "file_access_retries": 2,
        "socket_timeout": 25,
        "concurrent_fragment_downloads": 4,
        "merge_output_format": "mp4",
        "max_filesize": MAX_SIZE,
        "overwrites": True,
        "cachedir": False,
    }

    # Deno mavjud bo'lsa, YouTube JS ishlashiga yordam beradi
    if shutil.which("deno"):
        options["js_runtimes"] = {
            "deno": {}
        }

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    files = [
        p for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() in (
            ".mp4", ".mkv", ".webm", ".mov", ".m4v"
        )
    ]

    if not files:
        raise RuntimeError(
            "Video fayli topilmadi. Havola yoki platforma cheklovini tekshiring."
        )

    result = max(files, key=lambda p: p.stat().st_size)

    if result.stat().st_size > MAX_SIZE:
        raise RuntimeError("Video hajmi 45 MB dan katta.")

    return result


# ==============================
# VIDEO FORMATINI O'ZGARTIRISH
# ==============================

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
        "ffmpeg",
        "-y",
        "-i", str(source),
        "-vf", video_filter,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "27",
        "-c:a", "aac",
        "-b:a", "96k",
        "-threads", "2",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "1024",
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
            error.decode(errors="ignore")[-1000:]
        )

    if not output.exists():
        raise RuntimeError("Tayyor video yaratilmadi.")

    if output.stat().st_size > MAX_SIZE:
        raise RuntimeError("Tayyor video hajmi 45 MB dan katta.")

    return output


# ==============================
# TAYYOR VIDEONI YUBORISH
# ==============================

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

    except Exception as e:
        log.exception("CONVERSION ERROR")

        await status.edit_text(
            "❌ Video tayyorlashda xatolik:\n"
            + str(e)[:700]
        )


# ==============================
# START
# ==============================

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👋 ASAVED botiga xush kelibsiz!\n\n"
        "📱 9:16 video — videoni vertikal formatga o'tkazadi.\n"
        "⭕ Dumaloq video — Telegram dumaloq video qiladi.\n"
        "🔗 Video havolasi — YouTube, Instagram va TikTok videosini yuklaydi.\n\n"
        "Kerakli bo'limni tanlang:",
        reply_markup=keyboard
    )


# ==============================
# MENYU
# ==============================

@dp.message(F.text == "📱 9:16 video")
async def choose_vertical(message: Message):
    user_modes[message.from_user.id] = "916"

    await message.answer(
        "📹 9:16 qilmoqchi bo'lgan videongizni yuboring."
    )


@dp.message(F.text == "⭕ Dumaloq video")
async def choose_round(message: Message):
    user_modes[message.from_user.id] = "round"

    await message.answer(
        "⭕ Dumaloq qilmoqchi bo'lgan videongizni yuboring."
    )


@dp.message(F.text == "🔗 Video havolasi")
async def choose_link(message: Message):
    user_modes[message.from_user.id] = "link"

    await message.answer(
        "🔗 YouTube, Instagram yoki TikTok havolasini yuboring."
    )


# ==============================
# TELEFONDAN VIDEO
# ==============================

@dp.message(F.video)
async def uploaded_video(message: Message):
    uid = message.from_user.id
    mode = user_modes.get(uid)

    if mode not in ("916", "round"):
        await message.answer(
            "Avval video formatini tanlang.",
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

            await bot.download_file(
                file.file_path,
                destination=source
            )

            if source.stat().st_size > MAX_SIZE:
                raise RuntimeError("Video hajmi juda katta.")

            await status.delete()

            await send_converted(message, source, mode)

    except Exception as e:
        log.exception("UPLOAD ERROR")

        try:
            await status.edit_text(
                "❌ Videoni olishda xatolik:\n"
                + str(e)[:500]
            )
        except Exception:
            pass

    finally:
        user_modes.pop(uid, None)


# ==============================
# VIDEO FAYL
# ==============================

@dp.message(F.document)
async def uploaded_document(message: Message):
    doc = message.document

    if not doc.mime_type or not doc.mime_type.startswith("video/"):
        return

    uid = message.from_user.id
    mode = user_modes.get(uid)

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

            await bot.download_file(
                file.file_path,
                destination=source
            )

            await status.delete()

            await send_converted(message, source, mode)

    except Exception as e:
        log.exception("DOCUMENT ERROR")

        try:
            await status.edit_text(
                "❌ Xatolik:\n" + str(e)[:500]
            )
        except Exception:
            pass

    finally:
        user_modes.pop(uid, None)


# ==============================
# YOUTUBE / INSTAGRAM / TIKTOK
# ==============================

@dp.message(F.text)
async def link_handler(message: Message):
    url = (message.text or "").strip()

    if not valid_video_url(url):
        return

    status = await message.answer(
        "⏳ Video yuklanmoqda..."
    )

    try:
        async with DOWNLOAD_LIMIT:
            with tempfile.TemporaryDirectory() as tmp:
                source = await asyncio.to_thread(
                    download_media_sync,
                    url,
                    tmp
                )

                output = Path(tmp) / "vertical.mp4"

                await ffmpeg_convert(
                    source,
                    output,
                    "916"
                )

                await message.answer_video(
                    video=FSInputFile(output),
                    caption="✅ 9:16 video tayyor!"
                )

        await status.delete()

    except Exception as e:
        log.exception("LINK DOWNLOAD ERROR")

        try:
            await status.edit_text(
                "❌ Video yuklanmadi.\n\n"
                "Xatolik sababi:\n"
                + str(e)[:800]
            )
        except Exception:
            pass


# ==============================
# WEBHOOK
# ==============================

async def health(request):
    return web.Response(text="ASAVED OK")


async def webhook(request):
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
        log.exception("WEBHOOK ERROR")
        return web.Response(status=500)


async def on_startup(app):
    if not BASE_URL:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL topilmadi!"
        )

    await bot.set_webhook(
        url=f"{BASE_URL}/{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET
    )

    log.info("ASAVED BOT ISHGA TUSHDI")


async def on_shutdown(app):
    await bot.session.close()


app = web.Application()

app.router.add_get("/", health)
app.router.add_post(
    f"/{WEBHOOK_PATH}",
    webhook
)

app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)


if __name__ == "__main__":
    web.run_app(
        app,
        host="0.0.0.0",
        port=PORT
    )
