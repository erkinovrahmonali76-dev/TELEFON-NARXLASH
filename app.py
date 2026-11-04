import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Awaitable, Callable

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery,
)
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
    raise RuntimeError(
        "WEBHOOK_SECRET kamida 16 ta belgi boâlsin; faqat harf, raqam, _ yoki - ishlating"
    )
if not BASE_URL:
    raise RuntimeError(
        "RENDER_EXTERNAL_URL topilmadi. Render Web Service ishlatayotganingizni tekshiring"
    )

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

BTN_ROUND = "ð¥ Dumaloq video"
BTN_LINK = "ð Video havolasi"
BTN_916 = "ð± 9:16 format"

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_ROUND), KeyboardButton(text=BTN_916)],
        [KeyboardButton(text=BTN_LINK)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Kerakli funksiyani tanlang",
)

# Telegram Video Note haqiqiy dumaloq xabar sifatida 640x640 yuboriladi.
# 720p va 1080p esa sifatni saqlash uchun kvadrat MP4 video koârinishida yuboriladi.
round_quality_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="â­ Telegram dumaloq â 640p", callback_data="round_640")],
        [InlineKeyboardButton(text="â¨ Kvadrat MP4 â 720p", callback_data="round_720")],
        [InlineKeyboardButton(text="ð¥ Kvadrat MP4 â 1080p", callback_data="round_1080")],
    ]
)

subscribe_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="ð¢ Kanalga obuna boâlish", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="â Obunani tekshirish", callback_data="check_subscription")],
    ]
)

user_modes: dict[int, str] = {}
subscription_cache: dict[int, float] = {}
SUBSCRIPTION_CACHE_SECONDS = 30
VIDEO_SEMAPHORE = asyncio.Semaphore(2)

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
        "ð¥ <b>Dumaloq video</b> â 640p dumaloq xabar yoki 720p/1080p kvadrat MP4.\n"
        "ð± <b>9:16 format</b> â galereyadan video yuboring.\n"
        "ð <b>Video havolasi</b> â YouTube, Instagram yoki TikTok havolasini yuboring.\n\n"
        "Faqat oâzingizga tegishli yoki yuklab olishga ruxsatingiz bor videolardan foydalaning."
    )


async def is_subscribed(user_id: int, use_cache: bool = True) -> bool:
    now = asyncio.get_running_loop().time()
    cached_until = subscription_cache.get(user_id, 0)
    if use_cache and cached_until > now:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        subscribed = member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }
        if subscribed:
            subscription_cache[user_id] = now + SUBSCRIPTION_CACHE_SECONDS
        else:
            subscription_cache.pop(user_id, None)
        return subscribed
    except Exception:
        log.exception("Kanal obunasini tekshirishda xatolik")
        return False


async def send_subscription_required(message: Message):
    await message.answer(
        "â Botdan foydalanish uchun avval kanalimizga obuna boâling.\n"
        "Obuna boâlgach, Â«Obunani tekshirishÂ» tugmasini bosing.",
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
    if await is_subscribed(callback.from_user.id, use_cache=False):
        await callback.answer("â Obuna tasdiqlandi!", show_alert=False)
        if callback.message:
            await callback.message.answer(main_text(), reply_markup=keyboard)
    else:
        await callback.answer(
            "â Hali kanalga obuna boâlmagansiz. Avval obuna boâling!",
            show_alert=True,
        )


@router.callback_query(F.data.in_({"round_640", "round_720", "round_1080"}))
async def choose_round_quality(callback: CallbackQuery):
    if not callback.from_user:
        await callback.answer()
        return

    if not await is_subscribed(callback.from_user.id, use_cache=False):
        await callback.answer("Avval kanalga obuna boâling!", show_alert=True)
        return

    quality = callback.data.split("_", 1)[1]
    user_modes[callback.from_user.id] = f"round_{quality}"
    labels = {
        "640": "â­ Telegram dumaloq video (640p)",
        "720": "â¨ Kvadrat MP4 video (720p)",
        "1080": "ð¥ Kvadrat MP4 video (1080p)",
    }
    await callback.answer("Sifat tanlandi!")
    if callback.message:
        await callback.message.answer(
            f"{labels[quality]} tanlandi.\nEndi galereyadan 60 soniyagacha video yuboring."
        )


@router.message(CommandStart())
async def start(message: Message):
    user_modes.pop(message.from_user.id, None)
    await message.answer(main_text(), reply_markup=keyboard)


@router.message(F.text == BTN_ROUND)
async def choose_round(message: Message):
    user_modes.pop(message.from_user.id, None)
    await message.answer(
        "ð¥ Dumaloq video uchun sifatni tanlang:\n\n"
        "Eslatma: Telegramning haqiqiy dumaloq Video Note xabari 640p boâladi. "
        "720p va 1080p tiniq kvadrat MP4 video sifatida yuboriladi.",
        reply_markup=round_quality_keyboard,
    )


@router.message(F.text == BTN_916)
async def choose_916(message: Message):
    user_modes[message.from_user.id] = "916"
    await message.answer("ð± Endi 9:16 formatga oâtkazish uchun videoni yuboring.")


@router.message(F.text == BTN_LINK)
async def choose_link(message: Message):
    user_modes[message.from_user.id] = "link"
    await message.answer("ð YouTube, Instagram yoki TikTok video havolasini yuboring.")


def get_video_info(message: Message):
    if message.video:
        return (
            message.video.file_id,
            message.video.file_size,
            message.video.duration,
            "video.mp4",
        )
    if message.document and (message.document.mime_type or "").startswith("video/"):
        return (
            message.document.file_id,
            message.document.file_size,
            None,
            message.document.file_name or "video.mp4",
        )
    return None


async def download_telegram_video(message: Message, target: Path) -> tuple[Path, int | None]:
    info = get_video_info(message)
    if not info:
        raise ValueError("Video topilmadi. Iltimos, video fayl yuboring.")
    file_id, size, duration, filename = info
    if size and size > MAX_UPLOAD_BYTES:
        raise ValueError(
            "Video 20 MB dan katta. Telegram cloud Bot API getFile cheklovi sabab "
            "kichikroq video yuboring."
        )
    if duration and duration > MAX_SECONDS:
        raise ValueError("Video 10 daqiqadan uzun. Qisqaroq video yuboring.")
    tg_file = await bot.get_file(file_id)
    await bot.download_file(tg_file.file_path, destination=target)
    return target, duration


async def run_ffmpeg(args: list[str]):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            stderr.decode("utf-8", "ignore")[-1200:] or "FFmpeg xatoligi"
        )


async def make_round(src: Path, dst: Path, quality: int):
    if quality == 640:
        # Telegramning haqiqiy dumaloq Video Note formati.
        preset = "veryfast"
        crf = "20"
    elif quality == 720:
        preset = "fast"
        crf = "19"
    elif quality == 1080:
        preset = "fast"
        crf = "19"
    else:
        raise ValueError("Notoâgâri video sifati tanlandi.")

    video_filter = (
        f"scale={quality}:{quality}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={quality}:{quality},setsar=1"
    )

    await run_ffmpeg([
        "-i", str(src),
        "-t", "60",
        "-vf", video_filter,
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", crf,
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-threads", "0",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ac", "2",
        "-movflags", "+faststart",
        str(dst),
    ])


async def make_916(src: Path, dst: Path):
    await run_ffmpeg([
        "-i", str(src),
        "-vf",
        "scale=720:1280:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=720:1280,setsar=1",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "21",
        "-pix_fmt", "yuv420p",
        "-threads", "0",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(dst),
    ])


async def process_uploaded_video(message: Message, mode: str):
    status = await message.answer("â¡ Video sifatli qilib tayyorlanmoqda...")
    try:
        async with VIDEO_SEMAPHORE:
            with tempfile.TemporaryDirectory(prefix="asaved_") as tmp:
                folder = Path(tmp)
                src = folder / "input_video"
                dst = folder / "output.mp4"
                await download_telegram_video(message, src)

                if mode.startswith("round_"):
                    quality = int(mode.split("_", 1)[1])
                    await make_round(src, dst, quality)
                    if dst.stat().st_size > MAX_OUTPUT_BYTES:
                        raise ValueError(
                            "Tayyor video 49 MB dan katta chiqdi. "
                            "Qisqaroq video yuboring yoki pastroq sifatni tanlang."
                        )

                    if quality == 640:
                        await message.answer_video_note(
                            FSInputFile(dst),
                            length=640,
                        )
                    else:
                        await message.answer_video(
                            FSInputFile(dst),
                            caption=f"â Kvadrat MP4 video tayyor! Sifat: {quality}p",
                            supports_streaming=True,
                        )

                elif mode == "916":
                    await make_916(src, dst)
                    if dst.stat().st_size > MAX_OUTPUT_BYTES:
                        raise ValueError(
                            "Tayyor video 49 MB dan katta. Qisqaroq video yuboring."
                        )
                    await message.answer_video(
                        FSInputFile(dst),
                        caption="ð± 9:16 format tayyor!",
                        supports_streaming=True,
                    )
                else:
                    raise ValueError("Video rejimi topilmadi. Menyudan qayta tanlang.")

        await status.delete()
    except Exception as exc:
        log.exception("Video processing failed")
        await status.edit_text(f"â Xatolik: {str(exc)[:700]}")
    finally:
        if message.from_user:
            user_modes.pop(message.from_user.id, None)


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
    platform: str | None = None,
) -> Path:
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp oârnatilmagan. requirements.txt faylini tekshiring.")

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

    log.info("Video yuklanmoqda: platforma=%s", platform)
    await asyncio.to_thread(do_download)

    files = [
        p for p in folder.iterdir()
        if p.is_file() and p.name.startswith("link_video.")
    ]
    if not files:
        raise RuntimeError(
            "Video topilmadi yoki platforma yuklashga ruxsat bermadi."
        )

    result = max(files, key=lambda p: p.stat().st_size)
    if result.stat().st_size > MAX_LINK_BYTES:
        raise RuntimeError(
            "Video 49 MB dan katta. Boshqa, qisqaroq video havolasini yuboring."
        )
    return result


@router.message(F.text)
async def handle_text(message: Message):
    text = (message.text or "").strip()
    mode = user_modes.get(message.from_user.id)

    if mode == "link":
        if not valid_video_url(text):
            await message.answer(
                "â Faqat HTTPS YouTube, Instagram yoki TikTok video havolasini yuboring."
            )
            return

        status = await message.answer(
            "â¡ Video yuklanmoqda... Odatda 1 daqiqada tayyor boâlishiga harakat qiladi."
        )
        try:
            platform, platform_title = detect_platform(text)
            async with VIDEO_SEMAPHORE:
                with tempfile.TemporaryDirectory(prefix="asaved_link_") as tmp:
                    video_path = await download_public_link(
                        text, Path(tmp), platform
                    )
                    await message.answer_video(
                        FSInputFile(video_path),
                        caption=f"â {platform_title} videosi MP4 formatda tayyor!",
                        supports_streaming=True,
                    )
            await status.delete()
        except Exception:
            log.exception("Link download failed")
            await status.edit_text(
                "â Videoni olib boâlmadi.\n"
                "Sabab: havola yopiq boâlishi, platforma cheklovi yoki video hajmi boâlishi mumkin.\n"
                "Oâzingizga tegishli yoki yuklashga ruxsatingiz bor ochiq videoni sinab koâring."
            )
        finally:
            user_modes.pop(message.from_user.id, None)
        return

    if mode in ("916", "round_640", "round_720", "round_1080"):
        await message.answer(
            "Iltimos, havola emas, galereyadan video fayl yuboring."
        )
        return

    if valid_video_url(text):
        await message.answer(
            "Avval ð Video havolasi tugmasini bosing, keyin havolani yuboring."
        )
    else:
        await message.answer(
            "Menyudan kerakli funksiyani tanlang.",
            reply_markup=keyboard,
        )


@router.message(F.video | F.document)
async def handle_video(message: Message):
    mode = user_modes.get(message.from_user.id)
    allowed_modes = {"916", "round_640", "round_720", "round_1080"}

    if mode not in allowed_modes:
        await message.answer(
            "Avval ð¥ Dumaloq video yoki ð± 9:16 format tugmasini tanlang."
        )
        return

    if not get_video_info(message):
        await message.answer("Iltimos, video fayl yuboring.")
        return

    await process_uploaded_video(message, mode)


@router.message()
async def fallback(message: Message):
    await message.answer(
        "Menyudan kerakli funksiyani tanlang.",
        reply_markup=keyboard,
    )


async def on_startup():
    webhook_url = f"{BASE_URL}/{WEBHOOK_PATH}"
    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types(),
    )
    log.info("Webhook set: %s", webhook_url)


async def on_shutdown():
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.session.close()


async def main():
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_get(
        "/",
        lambda request: web.Response(text="ASAVED bot is running"),
    )
    app.router.add_get(
        "/health",
        lambda request: web.Response(text="ok"),
    )

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
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
