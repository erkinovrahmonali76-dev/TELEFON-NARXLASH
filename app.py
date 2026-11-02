import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, FSInputFile
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# -------------------- CONFIG --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook").strip().strip("/")
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

# Telegram cloud Bot API can download files up to 20 MB.
MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 49 * 1024 * 1024
MAX_VIDEO_SECONDS = 60 * 10
ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "instagram.com", "www.instagram.com"
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("asaved")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN Render Environment'ga kiritilmagan")
if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
    raise RuntimeError("WEBHOOK_SECRET kamida 16 belgidan iborat bo'lishi kerak")
if not re.fullmatch(r"[A-Za-z0-9_-]+", WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET faqat A-Z, a-z, 0-9, _ va - belgilaridan iborat bo'lsin")
if not BASE_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL topilmadi. Render Web Service ishlatayotganingizni tekshiring")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Per-user temporary workflow state. Files are removed after processing.
user_modes = {}
pending_links = {}

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎥 Dumaloq video"), KeyboardButton(text="⬇️ Video havolasi")],
        [KeyboardButton(text="📱 9:16 format"), KeyboardButton(text="📦 Hajmini kichraytirish")],
        [KeyboardButton(text="🔄 MP4 format"), KeyboardButton(text="✂️ Videoni kesish")],
        [KeyboardButton(text="🎵 Videodan MP3"), KeyboardButton(text="ℹ️ Yordam")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Bo'limni tanlang yoki video yuboring..."
)

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")]
    ])

def authorized_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Davom etish", callback_data="link_ok")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")]
    ])

def safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "video")
    return name[:80] or "video"

def valid_platform_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return parsed.scheme in ("http", "https") and any(
            host == domain or host.endswith("." + domain)
            for domain in ALLOWED_HOSTS
        )
    except Exception:
        return False

async def run_ffmpeg(args):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", "ignore")[-1200:] or "FFmpeg xatoligi")

async def get_video_file(message: Message, folder: Path) -> Path:
    media = message.video
    if not media and message.document and (message.document.mime_type or "").startswith("video/"):
        media = message.document
    if not media:
        raise ValueError("Iltimos, video fayl yuboring.")
    size = media.file_size or 0
    if size > MAX_INPUT_BYTES:
        raise ValueError("Video 20 MB dan katta. Hozircha 20 MB gacha bo'lgan video yuboring.")
    if not media.file_id:
        raise ValueError("Video faylini o'qib bo'lmadi.")
    file = await bot.get_file(media.file_id)
    if not file.file_path:
        raise ValueError("Telegram fayl manzilini qaytarmadi.")
    out = folder / "input_video"
    await bot.download_file(file.file_path, destination=out)
    if not out.exists() or out.stat().st_size == 0:
        raise ValueError("Video yuklab olinmadi.")
    return out

async def probe_duration(path: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    try:
        return float(stdout.decode().strip())
    except Exception:
        return 0.0

async def send_result(message: Message, path: Path, caption: str):
    size = path.stat().st_size
    if size > MAX_OUTPUT_BYTES:
        await message.answer("Tayyor fayl 49 MB dan katta chiqdi. Qisqaroq yoki kichikroq video yuboring.")
        return
    await message.answer_document(FSInputFile(path), caption=caption)

async def process_video(message: Message, mode: str, trim_range=None):
    status = await message.answer("⏳ Video qayta ishlanmoqda...")
    try:
        with tempfile.TemporaryDirectory(prefix="asaved_") as temp:
            folder = Path(temp)
            source = await get_video_file(message, folder)
            duration = await probe_duration(source)
            if duration and duration > MAX_VIDEO_SECONDS:
                raise ValueError("Video 10 daqiqadan uzun. Iltimos, qisqaroq video yuboring.")

            output = folder / "result.mp4"
            if mode == "circle":
                # Telegram video note: square 1:1, H.264, max 60 sec.
                if duration > 60:
                    raise ValueError("Dumaloq video Telegram'da 60 soniyagacha bo'lishi kerak.")
                await run_ffmpeg([
                    "-i", str(source), "-t", "60",
                    "-vf", "scale=640:640:force_original_aspect_ratio=increase,crop=640:640,fps=30",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
                    "-movflags", "+faststart", str(output)
                ])
                await status.delete()
                await message.answer_video_note(FSInputFile(output))
                return

            if mode == "vertical":
                vf = "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,fps=30"
                args = ["-i", str(source), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "26", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                        "-movflags", "+faststart", str(output)]
                caption = "✅ Video 9:16 formatga o'tkazildi."
            elif mode == "compress":
                args = ["-i", str(source), "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "29", "-c:a", "aac", "-b:a", "80k", "-movflags", "+faststart", str(output)]
                caption = "✅ Video hajmi kichraytirildi."
            elif mode == "mp4":
                args = ["-i", str(source), "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "25", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-movflags", "+faststart", str(output)]
                caption = "✅ Video MP4 formatga o'tkazildi."
            elif mode == "trim":
                start, end = trim_range
                args = ["-ss", start, "-i", str(source), "-to", end, "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "25", "-c:a", "aac",
                        "-movflags", "+faststart", str(output)]
                caption = "✅ Video kesildi."
            elif mode == "audio":
                audio = folder / "audio.mp3"
                await run_ffmpeg(["-i", str(source), "-vn", "-codec:a", "libmp3lame",
                                  "-q:a", "4", str(audio)])
                await status.delete()
                if audio.stat().st_size > MAX_OUTPUT_BYTES:
                    raise ValueError("MP3 fayl juda katta chiqdi.")
                await message.answer_audio(FSInputFile(audio), caption="🎵 Videodan audio ajratildi.")
                return
            else:
                raise ValueError("Noma'lum amal.")

            await run_ffmpeg(args)
            await status.delete()
            await send_result(message, output, caption)
    except ValueError as e:
        await status.edit_text(f"⚠️ {e}")
    except (TelegramBadRequest, TelegramNetworkError) as e:
        log.exception("Telegram media error")
        await status.edit_text("❌ Telegram faylni qabul qila olmadi. Video hajmini kamaytirib qayta urinib ko'ring.")
    except Exception:
        log.exception("Video processing failed")
        await status.edit_text("❌ Videoni qayta ishlashda xatolik. Boshqa video bilan urinib ko'ring.")

@router.message(CommandStart())
async def start(message: Message):
    user_modes.pop(message.from_user.id, None)
    await message.answer(
        "👋 <b>ASAVED BOT</b>ga xush kelibsiz!\n\n"
        "Videoni qayta ishlash uchun kerakli bo'limni tanlang. "
        "Yoki galereyadan video yuboring va avval bo'limni tanlang.",
        reply_markup=MAIN_KB
    )

@router.message(Command("help"))
@router.message(F.text == "ℹ️ Yordam")
async def help_message(message: Message):
    await message.answer(
        "<b>ASAVED imkoniyatlari</b>\n"
        "🎥 Dumaloq video — videoni Telegram video note qiladi (60 soniyagacha).\n"
        "📱 9:16 — vertikal formatga moslaydi.\n"
        "📦 Hajmini kichraytirish — video hajmini kamaytiradi.\n"
        "🔄 MP4 — MP4/H.264 formatga o'tkazadi.\n"
        "✂️ Kesish — vaqt oralig'ini kiritib videoni kesadi.\n"
        "🎵 MP3 — o'zingiz yuborgan videodan audio ajratadi.\n"
        "⬇️ Video havolasi — faqat o'zingizga tegishli yoki yuklab olishga ruxsatingiz bor videolar.\n\n"
        "Limit: Telegram orqali 20 MB gacha video yuboring. "
        "Maxfiy, pullik, DRM bilan himoyalangan yoki ruxsatsiz kontent yuklanmaydi.",
        reply_markup=MAIN_KB
    )

@router.callback_query(F.data == "cancel")
async def cancel(callback: CallbackQuery):
    pending_links.pop(callback.from_user.id, None)
    user_modes.pop(callback.from_user.id, None)
    await callback.answer("Bekor qilindi")
    if callback.message:
        await callback.message.edit_text("Bekor qilindi. Bosh menyudan bo'lim tanlang.")

@router.message(F.text == "🎥 Dumaloq video")
async def circle_mode(message: Message):
    user_modes[message.from_user.id] = ("circle", None)
    await message.answer("Galereyangizdan 60 soniyagacha video yuboring. Men uni dumaloq video qilib qaytaraman.", reply_markup=cancel_kb())

@router.message(F.text == "📱 9:16 format")
async def vertical_mode(message: Message):
    user_modes[message.from_user.id] = ("vertical", None)
    await message.answer("9:16 formatga o'tkazish uchun videoni yuboring.", reply_markup=cancel_kb())

@router.message(F.text == "📦 Hajmini kichraytirish")
async def compress_mode(message: Message):
    user_modes[message.from_user.id] = ("compress", None)
    await message.answer("Hajmini kichraytirish uchun videoni yuboring.", reply_markup=cancel_kb())

@router.message(F.text == "🔄 MP4 format")
async def mp4_mode(message: Message):
    user_modes[message.from_user.id] = ("mp4", None)
    await message.answer("MP4 formatga o'tkazish uchun videoni yuboring.", reply_markup=cancel_kb())

@router.message(F.text == "🎵 Videodan MP3")
async def audio_mode(message: Message):
    user_modes[message.from_user.id] = ("audio", None)
    await message.answer("Audio ajratish uchun o'zingiz yuborgan videoni jo'nating.", reply_markup=cancel_kb())

@router.message(F.text == "✂️ Videoni kesish")
async def trim_mode(message: Message):
    user_modes[message.from_user.id] = ("trim_wait_time", None)
    await message.answer(
        "Avval vaqt oralig'ini shu ko'rinishda yuboring:\n<code>00:00:05-00:00:20</code>\n\n"
        "Bu 5-soniyadan 20-soniyagacha kesadi. Keyin videoni yuboring.",
        reply_markup=cancel_kb()
    )

@router.message(F.text == "⬇️ Video havolasi")
async def link_mode(message: Message):
    user_modes[message.from_user.id] = ("link_wait", None)
    await message.answer(
        "YouTube, TikTok yoki Instagram'dagi videoning havolasini yuboring.\n\n"
        "Faqat o'zingiz yaratgan yoki yuklab olishga aniq ruxsatingiz bo'lgan videolar uchun. "
        "Maxfiy, login talab qiladigan, DRM/protected kontentni yuklab olish qo'llab-quvvatlanmaydi.",
        reply_markup=cancel_kb()
    )

@router.message(F.video | F.document)
async def video_received(message: Message):
    uid = message.from_user.id
    mode_data = user_modes.get(uid)
    if not mode_data:
        await message.answer("Avval menyudan kerakli bo'limni tanlang.", reply_markup=MAIN_KB)
        return
    mode, extra = mode_data
    if mode == "trim_wait_time":
        await message.answer("Avval vaqt oralig'ini yuboring, masalan 00:00:05-00:00:20.")
        return
    if mode == "link_wait":
        await message.answer("Bu bo'limda video havolasi kerak. Havolani matn ko'rinishida yuboring.")
        return
    if mode == "trim":
        user_modes.pop(uid, None)
        await process_video(message, "trim", extra)
    elif mode in {"circle", "vertical", "compress", "mp4", "audio"}:
        user_modes.pop(uid, None)
        await process_video(message, mode)
    else:
        await message.answer("Menyudan bo'lim tanlang.", reply_markup=MAIN_KB)

@router.message(F.text)
async def text_router(message: Message):
    uid = message.from_user.id
    mode_data = user_modes.get(uid)
    if not mode_data:
        return
    mode, extra = mode_data
    text = (message.text or "").strip()

    if mode == "trim_wait_time":
        match = re.fullmatch(r"(\d{2}:\d{2}:\d{2})\s*-\s*(\d{2}:\d{2}:\d{2})", text)
        if not match:
            await message.answer("Vaqt formati noto'g'ri. Masalan: <code>00:00:05-00:00:20</code>")
            return
        def to_seconds(t):
            h, m, s = map(int, t.split(":"))
            return h * 3600 + m * 60 + s
        start, end = match.groups()
        if to_seconds(end) <= to_seconds(start):
            await message.answer("Tugash vaqti boshlanish vaqtidan keyin bo'lishi kerak.")
            return
        user_modes[uid] = ("trim", (start, end))
        await message.answer("Endi kesiladigan videoni yuboring.", reply_markup=cancel_kb())
        return

    if mode == "link_wait":
        if not valid_platform_url(text):
            await message.answer("Faqat YouTube, TikTok yoki Instagram havolasini yuboring.")
            return
        pending_links[uid] = text
        await message.answer(
            "⚠️ Tasdiqlang: bu video o'zingizga tegishli yoki uni yuklab olish uchun ruxsatingiz bor. "
            "Men maxfiy, login talab qiladigan yoki himoyani aylanib o'tishni talab qiladigan videolarni yuklamayman.",
            reply_markup=authorized_kb()
        )
        return

@router.callback_query(F.data == "link_ok")
async def link_confirm(callback: CallbackQuery):
    uid = callback.from_user.id
    url = pending_links.pop(uid, None)
    if not url:
        await callback.answer("Havola topilmadi. Qayta yuboring.", show_alert=True)
        return
    await callback.answer("Yuklash boshlandi")
    status = await callback.message.answer("⏳ Ruxsat etilgan video yuklanmoqda...")
    try:
        with tempfile.TemporaryDirectory(prefix="asaved_link_") as temp:
            folder = Path(temp)
            outtmpl = str(folder / "%(title).60s.%(ext)s")
            opts = {
                "outtmpl": outtmpl,
                "format": "best[ext=mp4]/best",
                "merge_output_format": "mp4",
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 20,
                "retries": 1,
                "max_filesize": MAX_OUTPUT_BYTES,
                "restrictfilenames": True,
                "nocheckcertificate": False,
                "ignoreerrors": False,
            }
            # Do not pass cookies, credentials, or browser sessions.
            loop = asyncio.get_running_loop()
            def download():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
            await asyncio.wait_for(loop.run_in_executor(None, download), timeout=150)
            files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
            if not files:
                raise RuntimeError("Yuklab olinadigan ruxsat etilgan video topilmadi.")
            file = max(files, key=lambda p: p.stat().st_size)
            if file.stat().st_size > MAX_OUTPUT_BYTES:
                raise RuntimeError("Video 49 MB dan katta. Qisqaroq video tanlang.")
            await status.delete()
            await callback.message.answer_video(FSInputFile(file), caption="✅ Video tayyor.")
    except asyncio.TimeoutError:
        await status.edit_text("⏱ Yuklash vaqti tugadi. Keyinroq yoki boshqa havola bilan urinib ko'ring.")
    except Exception as e:
        log.warning("Link download failed: %s", e)
        await status.edit_text(
            "❌ Bu havoladagi videoni olishning imkoni bo'lmadi. "
            "Video ochiq va yuklab olishga ruxsat berilganini tekshiring yoki videoni galereyadan yuboring."
        )

@router.message()
async def fallback(message: Message):
    await message.answer("Menyudan kerakli bo'limni tanlang.", reply_markup=MAIN_KB)

async def on_startup(app: web.Application):
    webhook_url = f"{BASE_URL}/{WEBHOOK_PATH}"
    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types()
    )
    log.info("Webhook set: %s", webhook_url)

async def on_shutdown(app: web.Application):
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.session.close()

def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET
    ).register(app, path=f"/{WEBHOOK_PATH}")
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
