import asyncio
import logging
import os
import re
import tempfile
import html
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Awaitable, Callable

from aiohttp import web, ClientSession, ClientTimeout
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("asaved")

# ---------------------------------------------------------------------
# ENVIRONMENT
# ---------------------------------------------------------------------
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
        "WEBHOOK_SECRET kamida 16 ta belgi bo‘lsin; faqat harf, raqam, _ yoki - ishlating"
    )
if not BASE_URL:
    raise RuntimeError(
        "RENDER_EXTERNAL_URL topilmadi. Render Web Service ishlatayotganingizni tekshiring"
    )

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ---------------------------------------------------------------------
# MAIN MENU
# ---------------------------------------------------------------------
BTN_ROUND = "🎥 Dumaloq video"
BTN_LINK = "🔗 Video havolasi"
BTN_916 = "📱 9:16 format"
BTN_TTS = "🎙️ Matnni ovozga aylantirish"

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_ROUND), KeyboardButton(text=BTN_916)],
        [KeyboardButton(text=BTN_LINK)],
        [KeyboardButton(text=BTN_TTS)],
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

# ---------------------------------------------------------------------
# USER STATE / LIMITS
# ---------------------------------------------------------------------
user_modes: dict[int, str] = {}
subscription_cache: dict[int, float] = {}
SUBSCRIPTION_CACHE_SECONDS = 30
VIDEO_SEMAPHORE = asyncio.Semaphore(2)
TTS_SEMAPHORE = asyncio.Semaphore(2)
MAX_TTS_CHARS = 3000

# ---------------------------------------------------------------------
# ELEVENLABS TTS
# Set ELEVENLABS_API_KEY in Render Environment.
# Model defaults to eleven_multilingual_v2.
#
# The menu names are labels only. They do NOT claim to reproduce the
# named public figures. Each profile uses an independently authorized
# ElevenLabs voice ID supplied through its matching environment variable.
# If a profile ID is not set, the generic fallback voice is used.
# ---------------------------------------------------------------------
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
TTS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2").strip()

DEFAULT_MALE_VOICE_ID = os.getenv(
    "ELEVENLABS_MALE_VOICE_ID", "pNInz6obpgDQGcFmaJgB"
).strip()
DEFAULT_FEMALE_VOICE_ID = os.getenv(
    "ELEVENLABS_FEMALE_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"
).strip()

# Voice ID environment variable, visible menu label, fallback voice.
# Add each person's authorized voice ID in Render Environment if available.
VOICE_PROFILES = {
    "qodir": ("Qodir Tolipov", "ELEVENLABS_QODIR_VOICE_ID", DEFAULT_MALE_VOICE_ID),
    "xojiakbar": ("Xojiakbar", "ELEVENLABS_XOJIAKBAR_VOICE_ID", DEFAULT_MALE_VOICE_ID),
    "asilbek": ("Asilbek", "ELEVENLABS_ASILBEK_VOICE_ID", DEFAULT_MALE_VOICE_ID),
    "dili": ("Dili.me", "ELEVENLABS_DILI_VOICE_ID", DEFAULT_FEMALE_VOICE_ID),
    "davron": ("Davron Kabulov", "ELEVENLABS_DAVRON_VOICE_ID", DEFAULT_MALE_VOICE_ID),
    "mitti": ("Mittivine", "ELEVENLABS_MITTIVINE_VOICE_ID", DEFAULT_MALE_VOICE_ID),
    "shahzoda": ("Shahzoda", "ELEVENLABS_SHAHZODA_VOICE_ID", DEFAULT_FEMALE_VOICE_ID),
    "hamdam": ("Xamdam Sobirov", "ELEVENLABS_HAMDAM_VOICE_ID", DEFAULT_MALE_VOICE_ID),
    "alisher": ("Alisher Uzoqov", "ELEVENLABS_ALISHER_VOICE_ID", DEFAULT_MALE_VOICE_ID),
    "shoxrux": ("Shoxrux", "ELEVENLABS_SHOXRUX_VOICE_ID", DEFAULT_MALE_VOICE_ID),
    "chingiz": ("Chingizxon", "ELEVENLABS_CHINGIZ_VOICE_ID", DEFAULT_MALE_VOICE_ID),
}

VOICE_BUTTONS = {
    key: f"🎤 {profile[0]}"
    for key, profile in VOICE_PROFILES.items()
}
VOICE_BUTTON_TO_KEY = {button: key for key, button in VOICE_BUTTONS.items()}

voice_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=VOICE_BUTTONS["qodir"]), KeyboardButton(text=VOICE_BUTTONS["chingiz"])],
        [KeyboardButton(text=VOICE_BUTTONS["xojiakbar"]), KeyboardButton(text=VOICE_BUTTONS["asilbek"])],
        [KeyboardButton(text=VOICE_BUTTONS["dili"]), KeyboardButton(text=VOICE_BUTTONS["davron"])],
        [KeyboardButton(text=VOICE_BUTTONS["mitti"]), KeyboardButton(text=VOICE_BUTTONS["shahzoda"])],
        [KeyboardButton(text=VOICE_BUTTONS["hamdam"]), KeyboardButton(text=VOICE_BUTTONS["alisher"])],
        [KeyboardButton(text=VOICE_BUTTONS["shoxrux"])],
        [KeyboardButton(text="⬅️ Asosiy menyu")],
    ],
    resize_keyboard=True,
    input_field_placeholder="AI ovoz profilini tanlang"
)

# ---------------------------------------------------------------------
# VIDEO LIMITS
# ---------------------------------------------------------------------
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
        "🔗 <b>Video havolasi</b> — YouTube, Instagram yoki TikTok havolasini yuboring.\n"
        "🎙️ <b>Matnni ovozga aylantirish</b> — AI ovoz profilini tanlab, matnni MP3 qiling.\n\n"
        "Faqat o‘zingizga tegishli yoki yuklab olishga ruxsatingiz bor videolardan foydalaning."
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
    if await is_subscribed(callback.from_user.id, use_cache=False):
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


# ---------------------------------------------------------------------
# TTS HANDLERS
# ---------------------------------------------------------------------
@router.message(F.text == BTN_TTS)
async def choose_tts(message: Message):
    user_modes[message.from_user.id] = "voice_choose"
    await message.answer(
        "🎙️ Ovoz profilini tanlang.\n\n"
        "Eslatma: ko‘rsatilgan nomlar menyu yorliqlari. Ovozlar mustaqil "
        "AI profillar bo‘lib, mashhur insonlarning aynan ovozi deb taqdim etilmaydi.",
        reply_markup=voice_keyboard,
    )


@router.message(F.text.in_(set(VOICE_BUTTON_TO_KEY.keys())))
async def choose_voice(message: Message):
    voice_key = VOICE_BUTTON_TO_KEY[message.text]
    user_modes[message.from_user.id] = f"tts_{voice_key}"
    name = VOICE_PROFILES[voice_key][0]
    await message.answer(
        f"✅ {name} profili tanlandi.\n\nEndi ovozga aylantiriladigan matnni yuboring.",
        reply_markup=keyboard,
    )


@router.message(F.text == "⬅️ Asosiy menyu")
async def back_to_menu(message: Message):
    user_modes.pop(message.from_user.id, None)
    await message.answer(main_text(), reply_markup=keyboard)


async def text_to_speech(text: str, voice_id: str, destination: Path):
    """ElevenLabs API orqali MP3 yaratadi."""
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("Render Environment'da ELEVENLABS_API_KEY topilmadi")

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
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                detail = (await response.text())[:500]
                log.error("ElevenLabs API xatosi: status=%s, body=%s", response.status, detail)
                if response.status == 401:
                    raise RuntimeError("ElevenLabs API kaliti noto‘g‘ri yoki faol emas")
                if response.status == 429:
                    raise RuntimeError("ElevenLabs limiti yoki kreditlari tugagan bo‘lishi mumkin")
                if response.status == 422:
                    raise RuntimeError("Ovoz ID yoki matn/model sozlamasi yaroqsiz")
                raise RuntimeError(f"ElevenLabs API xatosi ({response.status})")
            audio_data = await response.read()

    if not audio_data:
        raise RuntimeError("ElevenLabs bo‘sh audio qaytardi")
    destination.write_bytes(audio_data)


async def process_tts(message: Message, text: str, mode: str):
    if len(text) > MAX_TTS_CHARS:
        await message.answer(f"Matn juda uzun. Eng ko‘pi {MAX_TTS_CHARS} ta belgi yuboring.")
        return
    if not text.strip():
        await message.answer("Ovozga aylantirish uchun matn yuboring.")
        return

    voice_key = mode.removeprefix("tts_")
    profile = VOICE_PROFILES.get(voice_key)
    if not profile:
        await message.answer("Ovoz profili topilmadi. Qaytadan tanlang.")
        user_modes.pop(message.from_user.id, None)
        return

    name, env_name, fallback_voice_id = profile
    voice_id = os.getenv(env_name, "").strip() or fallback_voice_id

    status = await message.answer("🎙️ Matn ovozga aylantirilmoqda...")
    try:
        async with TTS_SEMAPHORE:
            with tempfile.TemporaryDirectory(prefix="asaved_tts_") as tmp:
                audio_path = Path(tmp) / "asaved_voice.mp3"
                await text_to_speech(text, voice_id, audio_path)
                if not audio_path.exists() or audio_path.stat().st_size == 0:
                    raise RuntimeError("Audio fayl yaratilmadi")
                await message.answer_audio(
                    FSInputFile(audio_path),
                    title="ASAVED AI ovoz",
                    caption=f"✅ {name} profili orqali tayyorlandi",
                )
        await status.delete()
    except Exception as exc:
        log.exception("TTS generation failed")
        safe_error = html.escape(str(exc)[:500])
        await status.edit_text(f"❌ Ovoz yaratishda xatolik: {safe_error}")
    finally:
        user_modes.pop(message.from_user.id, None)


# ---------------------------------------------------------------------
# VIDEO PROCESSING
# ---------------------------------------------------------------------
def get_video_info(message: Message):
    if message.video:
        return message.video.file_id, message.video.file_size, message.video.duration, "video.mp4"
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
            "Video 20 MB dan katta. Telegram cloud Bot API getFile cheklovi sabab kichikroq video yuboring."
        )
    if duration and duration > MAX_SECONDS:
        raise ValueError("Video 10 daqiqadan uzun. Qisqaroq video yuboring.")
    tg_file = await bot.get_file(file_id)
    await bot.download_file(tg_file.file_path, destination=target)
    return target, duration


async def run_ffmpeg(args: list[str]):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", "ignore")[-1200:] or "FFmpeg xatoligi")


async def make_round(src: Path, dst: Path):
    await run_ffmpeg([
        "-i", str(src),
        "-t", "60",
        "-vf", "scale=640:640:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=640:640,setsar=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "0", "-crf", "27",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        "-movflags", "+faststart", str(dst),
    ])


async def make_916(src: Path, dst: Path):
    await run_ffmpeg([
        "-i", str(src),
        "-vf", "scale=720:1280:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=720:1280,setsar=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "0", "-crf", "26",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(dst),
    ])


async def process_uploaded_video(message: Message, mode: str):
    status = await message.answer("⚡ Video tezkor rejimda tayyorlanmoqda...")
    try:
        async with VIDEO_SEMAPHORE:
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
        await status.edit_text(f"❌ Xatolik: {html.escape(str(exc)[:700])}")
    finally:
        user_modes.pop(message.from_user.id, None)


# ---------------------------------------------------------------------
# LINK DOWNLOADER
# ---------------------------------------------------------------------
def valid_video_url(raw: str) -> bool:
    try:
        parsed = urlparse(raw.strip())
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and (
            host in ALLOWED_HOSTS or any(host.endswith("." + h) for h in ALLOWED_HOSTS)
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


async def download_public_link(url: str, folder: Path, platform: str | None = None) -> Path:
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp o‘rnatilmagan. requirements.txt faylini tekshiring.")

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
        raise RuntimeError("Video topilmadi yoki platforma yuklashga ruxsat bermadi.")
    result = max(files, key=lambda p: p.stat().st_size)
    if result.stat().st_size > MAX_LINK_BYTES:
        raise RuntimeError("Video 49 MB dan katta. Boshqa, qisqaroq video havolasini yuboring.")
    return result


# ---------------------------------------------------------------------
# TEXT / VIDEO ROUTERS
# ---------------------------------------------------------------------
@router.message(F.text)
async def handle_text(message: Message):
    text = (message.text or "").strip()
    mode = user_modes.get(message.from_user.id)

    if mode and mode.startswith("tts_") and mode != "tts_":
        await process_tts(message, text, mode)
        return

    if mode == "voice_choose":
        await message.answer(
            "Avval ro‘yxatdan AI ovoz profilini tanlang.",
            reply_markup=voice_keyboard,
        )
        return

    if mode == "link":
        if not valid_video_url(text):
            await message.answer(
                "❌ Faqat HTTPS YouTube, Instagram yoki TikTok video havolasini yuboring."
            )
            return
        status = await message.answer(
            "⚡ Video tezkor rejimda yuklanmoqda... Odatda 1 daqiqada tayyor bo‘lishiga harakat qiladi."
        )
        try:
            platform, platform_title = detect_platform(text)
            async with VIDEO_SEMAPHORE:
                with tempfile.TemporaryDirectory(prefix="asaved_link_") as tmp:
                    video_path = await download_public_link(text, Path(tmp), platform)
                    await message.answer_video(
                        FSInputFile(video_path),
                        caption=f"✅ {platform_title} videosi MP4 formatda tayyor!",
                    )
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
        await message.answer(
            "Avval 🔗 Video havolasi tugmasini bosing, keyin havolani yuboring."
        )
    else:
        await message.answer("Menyudan kerakli funksiyani tanlang.", reply_markup=keyboard)


@router.message(F.video | F.document)
async def handle_video(message: Message):
    mode = user_modes.get(message.from_user.id)
    if mode not in ("round", "916"):
        await message.answer(
            "Avval 🎥 Dumaloq video yoki 📱 9:16 format tugmasini tanlang."
        )
        return
    if not get_video_info(message):
        await message.answer("Iltimos, video fayl yuboring.")
        return
    await process_uploaded_video(message, mode)


@router.message()
async def fallback(message: Message):
    await message.answer("Menyudan kerakli funksiyani tanlang.", reply_markup=keyboard)


# ---------------------------------------------------------------------
# WEBHOOK
# ---------------------------------------------------------------------
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
    app.router.add_get("/", lambda request: web.Response(text="ASAVED bot is running"))
    app.router.add_get("/health", lambda request: web.Response(text="ok"))

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
