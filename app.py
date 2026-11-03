import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Awaitable, Callable

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    Video, Document, ReplyKeyboardRemove, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram import BaseMiddleware
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("asaved")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook").strip("/")
PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

CHANNEL_USERNAME = "@ReklamaBozoriUzz"
CHANNEL_URL = "https://t.me/ReklamaBozoriUzz"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN Environment Variable kiritilmagan")
if not WEBHOOK_SECRET or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET kamida 16 ta belgi bo‘lsin; faqat harf, raqam, _ yoki - ishlating")
if not BASE_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL topilmadi. Render Web Service ishlatayotganingizni tekshiring")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

BTN_ROUND = "🎥 Dumaloq video"
BTN_LINK = "🔗 Video havolasi"
BTN_916 = "📱 9:16 format"

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_ROUND), KeyboardButton(text=BTN_916)],
        [KeyboardButton(text=BTN_LINK)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Kerakli funksiyani tanlang"
)

subscribe_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📢 Kanalga obuna bo‘lish", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_subscription")],
    ]
)

# Har bir foydalanuvchining keyingi yuboradigan videosi uchun rejim.
user_modes: dict[int, str] = {}

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
}

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 49 * 1024 * 1024
MAX_LINK_BYTES = 49 * 1024 * 1024
MAX_SECONDS = 600


def main_text() -> str:
    return (
        "Assalomu alaykum! <b>ASAVED</b> botiga xush kelibsiz.\n\n"
        "🎥 <b>Dumaloq video</b> — galereyadan 60 soniyagacha video yuboring.\n"
        "📱 <b>9:16 format</b> — galereyadan video yuboring.\n"
        "🔗 <b>Video havolasi</b> — YouTube, Instagram yoki TikTok havolasini yuboring.\n\n"
        "Faqat o‘zingizga tegishli yoki yuklab olishga ruxsatingiz bor videolardan foydalaning."
    )


async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }
    except Exception:
        log.exception("Kanal obunasini tekshirishda xatolik")
        return False


async def send_subscription_required(message: Message):
    await message.answer(
        "⛔ Botdan foydalanish uchun avval kanalimizga obuna bo‘ling.\n"
        "Obuna bo‘lgach, «Obunani tekshirish» tugmasini bosing.",
        reply_markup=subscribe_keyboard,
    )


class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if not event.from_user:
            return await handler(event, data)

        if await is_subscribed(event.from_user.id):
            return await handler(event, data)

        await send_subscription_required(event)
        return None


router.message.middleware(SubscriptionMiddleware())


@router.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: CallbackQuery):
    if not callback.from_user:
        await callback.answer()
        return

    if await is_subscribed(callback.from_user.id):
        await callback.answer("✅ Obuna tasdiqlandi!", show_alert=False)
        if callback.message:
            await callback.message.answer(main_text(), reply_markup=keyboard)
    else:
        await callback.answer(
            "❌ Hali kanalga obuna bo‘lmagansiz. Avval obuna bo‘ling!",
            show_alert=True,
        )


@router.message(CommandStart())
async def start(message: Message):
    user_modes.pop(message.from_user.id, None)
    await message.answer(main_text(), reply_markup=keyboard)


@router.message(F.text == BTN_ROUND)
async def choose_round(message: Message):
    user_modes[message.from_user.id] = "round"
    await message.answer("🎥 Endi galereyadan 60 soniyagacha video yuboring.")


@router.message(F.text == BTN_916)
async def choose_916(message: Message):
    user_modes[message.from_user.id] = "916"
    await message.answer("📱 Endi 9:16 formatga o‘tkazish uchun videoni yuboring.")


@router.message(F.text == BTN_LINK)
async def choose_link(message: Message):
    user_modes[message.from_user.id] = "link"
    await message.answer("🔗 YouTube, Instagram yoki TikTok video havolasini yuboring.")


def get_video_info(message: Message):
    if message.video:
        return message.video.file_id, message.video.file_size, message.video.duration, "video.mp4"
    if message.document and (message.document.mime_type or "").startswith("video/"):
        return message.document.file_id, message.document.file_size, None, message.document.file_name or "video.mp4"
    return None


async def download_telegram_video(message: Message, target: Path) -> tuple[Path, int | None]:
    info = get_video_info(message)
    if not info:
        raise ValueError("Video topilmadi. Iltimos, video fayl yuboring.")
    file_id, size, duration, filename = info
    if size and size > MAX_UPLOAD_BYTES:
        raise ValueError("Video 20 MB dan katta. Telegram cloud Bot API getFile cheklovi sabab kichikroq video yuboring.")
    if duration and duration > MAX_SECONDS:
        raise ValueError("Video 10 daqiqadan uzun. Qisqaroq video yuboring.")
    tg_file = await bot.get_file(file_id)
    await bot.download_file(tg_file.file_path, destination=target)
    return target, duration


async def run_ffmpeg(args: list[str]):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", "ignore")[-1200:] or "FFmpeg xatoligi")


async def make_round(src: Path, dst: Path):
    await run_ffmpeg([
        "-i", str(src),
        "-t", "60",
        "-vf", "scale=640:640:force_original_aspect_ratio=increase,crop=640:640,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        "-movflags", "+faststart", str(dst)
    ])


async def make_916(src: Path, dst: Path):
    await run_ffmpeg([
        "-i", str(src),
        "-vf", "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(dst)
    ])


async def process_uploaded_video(message: Message, mode: str):
    status = await message.answer("⏳ Video tayyorlanmoqda...")
    try:
        with tempfile.TemporaryDirectory(prefix="asaved_") as tmp:
            folder = Path(tmp)
            src = folder / "input_video"
            dst = folder / "output.mp4"
            await download_telegram_video(message, src)
            if mode == "round":
                await make_round(src, dst)
                if dst.stat().st_size > MAX_OUTPUT_BYTES:
                    raise ValueError("Tayyor video juda katta chiqdi. Qisqaroq video yuboring.")
                await message.answer_video_note(FSInputFile(dst))
            else:
                await make_916(src, dst)
                if dst.stat().st_size > MAX_OUTPUT_BYTES:
                    raise ValueError("Tayyor video 49 MB dan katta. Qisqaroq video yuboring.")
                await message.answer_video(FSInputFile(dst), caption="📱 9:16 format tayyor!")
        await status.delete()
    except Exception as exc:
        log.exception("Video processing failed")
        await status.edit_text(f"❌ Xatolik: {str(exc)[:700]}")
    finally:
        user_modes.pop(message.from_user.id, None)


def valid_video_url(raw: str) -> bool:
    try:
        parsed = urlparse(raw.strip())
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and (
            host in ALLOWED_HOSTS or any(host.endswith("." + h) for h in ALLOWED_HOSTS)
        )
    except Exception:
        return False


async def download_public_link(url: str, folder: Path) -> Path:
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp o‘rnatilmagan. requirements.txt faylini tekshiring.")

    output_template = str(folder / "link_video.%(ext)s")
    options = {
        "format": "best[ext=mp4][height<=1080]/best[height<=1080]/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "max_filesize": MAX_LINK_BYTES,
        "socket_timeout": 25,
        "retries": 2,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }

    def do_download():
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

    await asyncio.to_thread(do_download)
    files = [p for p in folder.iterdir() if p.is_file() and p.name.startswith("link_video.")]
    if not files:
        raise RuntimeError("Video topilmadi yoki platforma yuklashga ruxsat bermadi.")
    result = max(files, key=lambda p: p.stat().st_size)
    if result.stat().st_size > MAX_LINK_BYTES:
        raise RuntimeError("Video 49 MB dan katta. Boshqa, qisqaroq video havolasini yuboring.")
    return result


@router.message(F.text)
async def handle_text(message: Message):
    text = (message.text or "").strip()
    mode = user_modes.get(message.from_user.id)

    if mode == "link":
        if not valid_video_url(text):
            await message.answer("❌ Faqat HTTPS YouTube, Instagram yoki TikTok video havolasini yuboring.")
            return
        status = await message.answer("⏳ Havola tekshirilmoqda va video olinmoqda...")
        try:
            with tempfile.TemporaryDirectory(prefix="asaved_link_") as tmp:
                video_path = await download_public_link(text, Path(tmp))
                await message.answer_video(FSInputFile(video_path), caption="✅ Video tayyor!")
            await status.delete()
        except Exception:
            log.exception("Link download failed")
            await status.edit_text(
                "❌ Videoni olib bo‘lmadi.\n"
                "Sabab: havola yopiq bo‘lishi, platforma cheklovi yoki video hajmi bo‘lishi mumkin.\n"
                "O‘zingizga tegishli yoki yuklashga ruxsatingiz bor ochiq videoni sinab ko‘ring."
            )
        finally:
            user_modes.pop(message.from_user.id, None)
        return

    if mode in ("round", "916"):
        await message.answer("Iltimos, havola emas, galereyadan video fayl yuboring.")
        return

    if valid_video_url(text):
        await message.answer("Avval 🔗 Video havolasi tugmasini bosing, keyin havolani yuboring.")
    else:
        await message.answer("Menyudan kerakli funksiyani tanlang.", reply_markup=keyboard)


@router.message(F.video | F.document)
async def handle_video(message: Message):
    mode = user_modes.get(message.from_user.id)
    if mode not in ("round", "916"):
        await message.answer("Avval 🎥 Dumaloq video yoki 📱 9:16 format tugmasini tanlang.")
        return
    if not get_video_info(message):
        await message.answer("Iltimos, video fayl yuboring.")
        return
    await process_uploaded_video(message, mode)


@router.message()
async def fallback(message: Message):
    await message.answer("Menyudan kerakli funksiyani tanlang.", reply_markup=keyboard)


async def on_startup():
    webhook_url = f"{BASE_URL}/{WEBHOOK_PATH}"
    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types()
    )
    log.info("Webhook set: %s", webhook_url)


async def on_shutdown():
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.session.close()


async def main():
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_get("/", lambda request: web.Response(text="ASAVED bot is running"))
    app.router.add_get("/health", lambda request: web.Response(text="ok"))
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET
    ).register(app, path=f"/{WEBHOOK_PATH}")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(lambda app: on_startup())
    app.on_cleanup.append(lambda app: on_shutdown())
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("ASAVED listening on port %s", PORT)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
