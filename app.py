import asyncio
import base64
import html
import io
import logging
import os
import re
import tempfile
import time
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
        "WEBHOOK_SECRET kamida 16 ta belgi bo'lsin; faqat harf, raqam, _ yoki - ishlating"
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
BTN_AI_IMAGE = "🖼️ AI rasm yaratish"
BTN_AI_VIDEO = "🎬 AI video yaratish"
BTN_IMAGE_VIDEO = "✨ Rasmni videoga aylantirish"
BTN_BACK = "⬅️ Asosiy menyu"

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_ROUND), KeyboardButton(text=BTN_916)],
        [KeyboardButton(text=BTN_LINK)],
        [KeyboardButton(text=BTN_TTS)],
        [KeyboardButton(text=BTN_AI_IMAGE), KeyboardButton(text=BTN_AI_VIDEO)],
        [KeyboardButton(text=BTN_IMAGE_VIDEO)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Kerakli funksiyani tanlang"
)

subscribe_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📢 Kanalga obuna bo'lish", url=CHANNEL_URL)],
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
AI_MEDIA_SEMAPHORE = asyncio.Semaphore(1)
MAX_AI_PROMPT_CHARS = 1500
MAX_AI_IMAGE_BYTES = 20 * 1024 * 1024
MAX_AI_OUTPUT_BYTES = 49 * 1024 * 1024
# Image-to-video uchun foydalanuvchi yuborgan rasm vaqtincha xotirada turadi.
pending_ai_images: dict[int, tuple[bytes, str]] = {}

# ---------------------------------------------------------------------
# ELEVENLABS AI VOICES
# Bot foydalanuvchiga ElevenLabs hisobida mavjud bo'lgan ovozlarni ko'rsatadi.
# Har bir tugma o'z voice_id sini ishlatadi; barcha ovozlar bir xil emas.
# ---------------------------------------------------------------------
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
TTS_MODEL_ID = os.getenv(
    "ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"
).strip()

AI_VOICES_CACHE: list[dict[str, str]] = []
AI_VOICES_CACHE_TIME = 0.0
AI_VOICES_CACHE_TTL = 300  # 5 daqiqa
VOICES_PER_PAGE = 30


def voice_gender(voice: dict) -> str:
    labels = voice.get("labels") or {}
    gender = str(labels.get("gender", "")).strip().lower()
    if gender in ("female", "woman"):
        return "female"
    if gender in ("male", "man"):
        return "male"
    return "other"


async def load_ai_voices(force: bool = False) -> list[dict[str, str]]:
    """ElevenLabs hisobida mavjud bo'lgan AI ovozlarni oladi va 5 daqiqa keshlaydi."""
    global AI_VOICES_CACHE, AI_VOICES_CACHE_TIME

    if not ELEVENLABS_API_KEY:
        raise RuntimeError(
            "Render Environment'da ELEVENLABS_API_KEY kiritilmagan."
        )

    now = time.monotonic()
    if (
        not force
        and AI_VOICES_CACHE
        and now - AI_VOICES_CACHE_TIME < AI_VOICES_CACHE_TTL
    ):
        return AI_VOICES_CACHE

    url = "https://api.elevenlabs.io/v1/voices"
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    timeout = ClientTimeout(total=40, connect=15)

    async with ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                detail = (await response.text())[:400]
                log.error("ElevenLabs voices API: %s %s", response.status, detail)
                if response.status == 401:
                    raise RuntimeError("ElevenLabs API kaliti noto'g'ri yoki faol emas.")
                if response.status == 429:
                    raise RuntimeError("ElevenLabs so'rovlar limiti vaqtincha tugagan.")
                raise RuntimeError(f"ElevenLabs ovozlar API xatosi: {response.status}")
            data = await response.json()

    voices = []
    seen_ids = set()

    for item in data.get("voices", []):
        voice_id = str(item.get("voice_id") or "").strip()
        name = str(item.get("name") or "Nomsiz AI ovoz").strip()

        if not voice_id or voice_id in seen_ids:
            continue
        # Telegram callback_data uzunligi 64 baytdan oshmasin.
        if len(("voice:" + voice_id).encode("utf-8")) > 64:
            log.warning("Voice ID callback uchun juda uzun, o'tkazib yuborildi: %s", name)
            continue

        seen_ids.add(voice_id)
        voices.append({
            "id": voice_id,
            "name": name[:45],
            "gender": voice_gender(item),
        })

    if not voices:
        raise RuntimeError(
            "ElevenLabs hisobingizda foydalanish mumkin bo'lgan ovozlar topilmadi."
        )

    AI_VOICES_CACHE = voices
    AI_VOICES_CACHE_TIME = time.monotonic()
    return voices


def make_voice_keyboard(
    voices: list[dict[str, str]],
    page: int = 0
) -> InlineKeyboardMarkup:
    start = page * VOICES_PER_PAGE
    page_voices = voices[start:start + VOICES_PER_PAGE]
    rows = []
    row = []

    for voice in page_voices:
        if voice["gender"] == "female":
            icon = "👩"
        elif voice["gender"] == "male":
            icon = "👨"
        else:
            icon = "🎙️"

        label = f"{icon} {voice['name']}"
        row.append(
            InlineKeyboardButton(
                text=label[:60],
                callback_data=f"voice:{voice['id']}"
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    total_pages = max(1, (len(voices) + VOICES_PER_PAGE - 1) // VOICES_PER_PAGE)
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Oldingi",
                callback_data=f"voicepage:{page - 1}"
            )
        )
    if page + 1 < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="Keyingi ➡️",
                callback_data=f"voicepage:{page + 1}"
            )
        )
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        "🎙️ <b>Matnni ovozga aylantirish</b> — AI ovoz tanlab, matnni MP3 qiling.\n"
        "🖼️ <b>AI rasm yaratish</b> — matndan rasm yarating.\n"
        "🎬 <b>AI video yaratish</b> — matndan qisqa video yarating.\n"
        "✨ <b>Rasmni videoga aylantirish</b> — rasm yuborib, uni jonlantiring.\n\n"
        "Faqat o'zingizga tegishli yoki yuklab olishga ruxsatingiz bor videolardan foydalaning."
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
        "⛔ Botdan foydalanish uchun avval kanalimizga obuna bo'ling.\n"
        "Obuna bo'lgach, «Obunani tekshirish» tugmasini bosing.",
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
router.callback_query.middleware(SubscriptionMiddleware())


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
            "❌ Hali kanalga obuna bo'lmagansiz. Avval obuna bo'ling!",
            show_alert=True,
        )


@router.message(CommandStart())
async def start(message: Message):
    user_modes.pop(message.from_user.id, None)
    pending_ai_images.pop(message.from_user.id, None)
    await message.answer(main_text(), reply_markup=keyboard)


@router.message(F.text == BTN_ROUND)
async def choose_round(message: Message):
    user_modes[message.from_user.id] = "round"
    await message.answer("🎥 Endi galereyadan 60 soniyagacha video yuboring.")


@router.message(F.text == BTN_916)
async def choose_916(message: Message):
    user_modes[message.from_user.id] = "916"
    await message.answer("📱 Endi 9:16 formatga o'tkazish uchun videoni yuboring.")


@router.message(F.text == BTN_LINK)
async def choose_link(message: Message):
    user_modes[message.from_user.id] = "link"
    await message.answer("🔗 YouTube, Instagram yoki TikTok video havolasini yuboring.")


# ---------------------------------------------------------------------
# TTS HANDLERS
# ---------------------------------------------------------------------
@router.message(F.text == BTN_TTS)
async def choose_tts(message: Message):
    status = await message.answer("🔎 ElevenLabs AI ovozlari yuklanmoqda...")
    try:
        voices = await load_ai_voices(force=True)
        await status.delete()
        total_pages = max(1, (len(voices) + VOICES_PER_PAGE - 1) // VOICES_PER_PAGE)
        await message.answer(
            f"🎙️ Hisobingizdan {len(voices)} ta AI ovoz topildi.\n"
            f"1-sahifa / {total_pages}\n\n"
            "👩 Qiz va 👨 o'g'il ovozlari alohida ko'rsatiladi. "
            "O'zingizga yoqqan ismli ovozni tanlang.",
            reply_markup=make_voice_keyboard(voices, 0)
        )
        user_modes[message.from_user.id] = "voice_choose"
    except Exception as exc:
        log.exception("AI ovozlar ro'yxatini olishda xatolik")
        await status.edit_text(
            f"❌ {html.escape(str(exc)[:500])}"
        )


@router.callback_query(F.data.startswith("voicepage:"))
async def change_voice_page(callback: CallbackQuery):
    try:
        page = int(callback.data.split(":", 1)[1])
        voices = await load_ai_voices()
        total_pages = max(1, (len(voices) + VOICES_PER_PAGE - 1) // VOICES_PER_PAGE)

        if page < 0 or page >= total_pages:
            await callback.answer("Bu sahifa mavjud emas.", show_alert=True)
            return

        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=make_voice_keyboard(voices, page)
            )
        await callback.answer(f"{page + 1}-sahifa")
    except Exception:
        log.exception("Ovoz sahifasini almashtirish xatosi")
        await callback.answer("Ovozlar ro'yxatini yangilab bo'lmadi.", show_alert=True)


@router.callback_query(F.data.startswith("voice:"))
async def select_ai_voice(callback: CallbackQuery):
    voice_id = callback.data.split(":", 1)[1]
    try:
        voices = await load_ai_voices()
        selected = next((v for v in voices if v["id"] == voice_id), None)

        if not selected:
            await callback.answer(
                "Bu ovoz endi mavjud emas. Ro'yxatni qayta oching.",
                show_alert=True
            )
            return

        user_modes[callback.from_user.id] = f"tts:{voice_id}"

        if callback.message:
            await callback.message.answer(
                f"✅ <b>{html.escape(selected['name'])}</b> ovozi tanlandi.\n\n"
                "Endi gapirtirmoqchi bo'lgan matningizni yuboring.",
                reply_markup=keyboard
            )
        await callback.answer("Ovoz tanlandi!")
    except Exception:
        log.exception("AI ovoz tanlash xatosi")
        await callback.answer("Ovoz tanlashda xatolik bo'ldi.", show_alert=True)


@router.message(F.text == BTN_BACK)
async def back_to_menu(message: Message):
    user_modes.pop(message.from_user.id, None)
    pending_ai_images.pop(message.from_user.id, None)
    await message.answer(main_text(), reply_markup=keyboard)


async def text_to_speech(text: str, voice_id: str, destination: Path):
    """ElevenLabs tayyor AI ovozida matnni MP3 ga aylantiradi."""
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
                log.error(
                    "ElevenLabs TTS xatosi: status=%s, body=%s",
                    response.status, detail
                )
                if response.status == 401:
                    raise RuntimeError("ElevenLabs API kaliti noto'g'ri yoki faol emas.")
                if response.status == 429:
                    raise RuntimeError(
                        "ElevenLabs limiti yoki kreditlari tugagan bo'lishi mumkin."
                    )
                if response.status == 422:
                    raise RuntimeError(
                        "Tanlangan ovoz yoki model bu matn uchun mos emas."
                    )
                raise RuntimeError(f"ElevenLabs API xatosi ({response.status})")

            audio_data = await response.read()

    if not audio_data:
        raise RuntimeError("ElevenLabs bo'sh audio qaytardi")
    destination.write_bytes(audio_data)


async def process_tts(message: Message, text: str, mode: str):
    if len(text) > MAX_TTS_CHARS:
        await message.answer(
            f"Matn juda uzun. Eng ko'pi {MAX_TTS_CHARS} ta belgi yuboring."
        )
        return
    if not text.strip():
        await message.answer("Ovozga aylantirish uchun matn yuboring.")
        return

    voice_id = mode.removeprefix("tts:")
    valid_ids = {voice["id"] for voice in await load_ai_voices()}
    if voice_id not in valid_ids:
        user_modes.pop(message.from_user.id, None)
        await message.answer(
            "Tanlangan ovoz topilmadi. Iltimos, ovozni qaytadan tanlang.",
            reply_markup=keyboard
        )
        return

    status = await message.answer("🎙️ Tanlangan AI ovoz matningizni gapiryapti...")
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
                    caption="✅ Tanlangan AI ovozda tayyor!"
                )

        await status.delete()
    except Exception as exc:
        log.exception("TTS generation failed")
        await status.edit_text(
            f"❌ Ovoz yaratishda xatolik: {html.escape(str(exc)[:500])}"
        )
    finally:
        user_modes.pop(message.from_user.id, None)


# ---------------------------------------------------------------------
# ELEVENLABS IMAGE & VIDEO GENERATION
# ---------------------------------------------------------------------
ELEVENLABS_FLOWS_URL = "https://api.elevenlabs.io/v1/flows"
AI_IMAGE_MODEL = os.getenv("ELEVENLABS_IMAGE_MODEL", "gemini-3-pro-image").strip()
AI_VIDEO_MODEL = os.getenv(
    "ELEVENLABS_VIDEO_MODEL", "veo-3.1-fast-generate-001"
).strip()


def require_elevenlabs_key():
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("Render Environment'da ELEVENLABS_API_KEY kiritilmagan.")


async def elevenlabs_json_request(method: str, url: str, payload: dict | None = None) -> dict:
    require_elevenlabs_key()
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Accept": "application/json"}
    timeout = ClientTimeout(total=90, connect=20)
    async with ClientSession(timeout=timeout) as session:
        async with session.request(method, url, headers=headers, json=payload) as response:
            if response.status not in (200, 201):
                detail = (await response.text())[:900]
                log.error("ElevenLabs %s %s: %s %s", method, url, response.status, detail)
                if response.status == 401:
                    raise RuntimeError("API kaliti noto'g'ri yoki faol emas (401).")
                if response.status == 402:
                    raise RuntimeError(
                        "Image & Video API uchun Pro yoki undan yuqori tarif va yetarli kredit kerak (402)."
                    )
                if response.status == 403:
                    raise RuntimeError(
                        "API kalitida Image & Video yoki Flows ruxsati yoqilmagan (403)."
                    )
                if response.status == 422:
                    raise RuntimeError("Model parametrlari mos kelmadi (422). Model va formatni tekshiring.")
                if response.status == 429:
                    raise RuntimeError("ElevenLabs so'rov limiti vaqtincha tugagan (429). Keyinroq urinib ko'ring.")
                raise RuntimeError(f"ElevenLabs API xatosi: HTTP {response.status}. {detail[:250]}")
            return await response.json()


async def download_generation_result(content_url: str, destination: Path):
    timeout = ClientTimeout(total=180, connect=20)
    async with ClientSession(timeout=timeout) as session:
        async with session.get(content_url) as response:
            if response.status != 200:
                raise RuntimeError(f"Tayyor faylni yuklab bo'lmadi: HTTP {response.status}")
            data = await response.read()
    if not data:
        raise RuntimeError("AI bo'sh fayl qaytardi.")
    if len(data) > MAX_AI_OUTPUT_BYTES:
        raise RuntimeError("AI yaratgan fayl Telegram yuborish chegarasidan katta.")
    destination.write_bytes(data)


async def wait_for_generation(kind: str, generation_id: str, status_message: Message) -> dict:
    """Image: 2s oralig'ida, video: 10s oralig'ida statusni tekshiradi."""
    interval = 2 if kind == "image" else 10
    max_wait = 150 if kind == "image" else 420
    endpoint = f"{ELEVENLABS_FLOWS_URL}/{kind}/{generation_id}"
    started = time.monotonic()
    last_text = ""
    while time.monotonic() - started < max_wait:
        await asyncio.sleep(interval)
        result = await elevenlabs_json_request("GET", endpoint)
        state = str(result.get("status", "")).lower()
        if state == "completed":
            return result
        if state == "failed":
            reason = result.get("failure_reason", "unknown")
            detail = result.get("error_message", "Generatsiya yakunlanmadi.")
            raise RuntimeError(f"Generatsiya bajarilmadi ({reason}): {detail}")
        elapsed = int(time.monotonic() - started)
        new_text = f"⏳ AI {'rasm' if kind == 'image' else 'video'} tayyorlayapti... {elapsed} soniya"
        if new_text != last_text:
            try:
                await status_message.edit_text(new_text)
            except Exception:
                pass
            last_text = new_text
    raise TimeoutError("Generatsiya ko'p vaqt oldi. Birozdan keyin qayta urinib ko'ring.")


async def create_ai_image(prompt: str, message: Message):
    if not prompt.strip() or len(prompt) > MAX_AI_PROMPT_CHARS:
        raise ValueError(f"Matn 1–{MAX_AI_PROMPT_CHARS} belgi oralig'ida bo'lishi kerak.")
    payload = {
        "model_id": AI_IMAGE_MODEL,
        "prompt": prompt,
        "aspect_ratio": "9:16",
        "resolution": "1K",
    }
    created = await elevenlabs_json_request("POST", f"{ELEVENLABS_FLOWS_URL}/image", payload)
    result = await wait_for_generation("image", created["id"], message)
    return result["content_url"]


async def create_ai_video(prompt: str, message: Message, start_frame: dict | None = None):
    if not prompt.strip() or len(prompt) > MAX_AI_PROMPT_CHARS:
        raise ValueError(f"Matn 1–{MAX_AI_PROMPT_CHARS} belgi oralig'ida bo'lishi kerak.")
    payload = {
        "model_id": AI_VIDEO_MODEL,
        "prompt": prompt,
        "duration_secs": 8,
        "aspect_ratio": "9:16",
        "resolution": "720p",
        "generate_audio": True,
    }
    if start_frame:
        payload["start_frame"] = start_frame
    created = await elevenlabs_json_request("POST", f"{ELEVENLABS_FLOWS_URL}/video", payload)
    result = await wait_for_generation("video", created["id"], message)
    return result["content_url"]


async def deliver_ai_image(message: Message, prompt: str):
    status = await message.answer("🖼️ AI rasm yaratish boshlandi...")
    try:
        async with AI_MEDIA_SEMAPHORE:
            content_url = await create_ai_image(prompt, status)
            with tempfile.TemporaryDirectory(prefix="asaved_ai_image_") as tmp:
                path = Path(tmp) / "asaved_ai.png"
                await download_generation_result(content_url, path)
                await message.answer_photo(FSInputFile(path), caption="✅ AI rasm tayyor!")
        await status.delete()
    except Exception as exc:
        log.exception("AI image generation failed")
        await status.edit_text(f"❌ Rasm yaratishda xatolik: {html.escape(str(exc)[:600])}")
    finally:
        user_modes.pop(message.from_user.id, None)


async def deliver_ai_video(message: Message, prompt: str, start_frame: dict | None = None):
    status = await message.answer("🎬 AI video yaratish boshlandi. Bu bir necha daqiqa olishi mumkin...")
    try:
        async with AI_MEDIA_SEMAPHORE:
            content_url = await create_ai_video(prompt, status, start_frame)
            with tempfile.TemporaryDirectory(prefix="asaved_ai_video_") as tmp:
                path = Path(tmp) / "asaved_ai.mp4"
                await download_generation_result(content_url, path)
                await message.answer_video(
                    FSInputFile(path),
                    caption="✅ AI video tayyor! (9:16, 8 soniya)"
                )
        await status.delete()
    except Exception as exc:
        log.exception("AI video generation failed")
        await status.edit_text(f"❌ Video yaratishda xatolik: {html.escape(str(exc)[:600])}")
    finally:
        user_modes.pop(message.from_user.id, None)
        pending_ai_images.pop(message.from_user.id, None)


@router.message(F.text == BTN_AI_IMAGE)
async def choose_ai_image(message: Message):
    user_modes[message.from_user.id] = "ai_image"
    pending_ai_images.pop(message.from_user.id, None)
    await message.answer(
        "🖼️ Qanday rasm kerakligini yozing. Masalan:\n"
        "<i>Oq sport mashinasi, tun, yomg'irli Toshkent ko'chasi, cinematic, 9:16</i>\n\n"
        "Bekor qilish: ⬅️ Asosiy menyu"
    )


@router.message(F.text == BTN_AI_VIDEO)
async def choose_ai_video(message: Message):
    user_modes[message.from_user.id] = "ai_video"
    pending_ai_images.pop(message.from_user.id, None)
    await message.answer(
        "🎬 Qanday video kerakligini yozing. AI 9:16 formatda 8 soniyagacha video yaratadi.\n"
        "Masalan: <i>Kamera sekin yaqinlashadi, yomg'irli ko'cha, cinematic, realistik.</i>"
    )


@router.message(F.text == BTN_IMAGE_VIDEO)
async def choose_image_video(message: Message):
    user_modes[message.from_user.id] = "ai_i2v_image"
    pending_ai_images.pop(message.from_user.id, None)
    await message.answer("✨ Endi jonlantirmoqchi bo'lgan rasmingizni yuboring (JPG, PNG yoki WEBP).")


@router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def receive_ai_image(message: Message):
    uid = message.from_user.id
    mode = user_modes.get(uid)
    if mode != "ai_i2v_image":
        return

    try:
        if message.photo:
            file_id = message.photo[-1].file_id
            mime_type = "image/jpeg"
        elif message.document:
            file_id = message.document.file_id
            mime_type = message.document.mime_type or "image/jpeg"
        else:
            return
        tg_file = await bot.get_file(file_id)
        buffer = io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=buffer)
        image_bytes = buffer.getvalue()
        if not image_bytes:
            raise ValueError("Rasm fayli bo'sh.")
        if mime_type not in {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}:
            raise ValueError("Faqat JPG, PNG, WEBP, HEIC yoki HEIF rasmlarini yuboring.")
        if len(image_bytes) > MAX_AI_IMAGE_BYTES:
            raise ValueError("Rasm 20 MB dan katta. Kichikroq rasm yuboring.")
        pending_ai_images[uid] = (image_bytes, mime_type)
        user_modes[uid] = "ai_i2v_prompt"
        await message.answer("✅ Rasm olindi! Endi unda qanday harakat bo'lishini yozing. Masalan: <i>Kamera yaqinlashsin, shamol sochlarni yengil harakatlantirsin.</i>")
    except Exception as exc:
        log.exception("Image upload failed")
        await message.answer(f"❌ Rasmni olishda xatolik: {html.escape(str(exc)[:300])}")


# ---------------------------------------------------------------------
# VIDEO PROCESSING
# ---------------------------------------------------------------------
def get_video_info(message: Message):
    if message.video:
        return (
            message.video.file_id,
            message.video.file_size,
            message.video.duration,
            "video.mp4"
        )
    if message.document and (message.document.mime_type or "").startswith("video/"):
        return (
            message.document.file_id,
            message.document.file_size,
            None,
            message.document.file_name or "video.mp4"
        )
    return None


async def download_telegram_video(message: Message, target: Path) -> tuple[Path, int | None]:
    info = get_video_info(message)
    if not info:
        raise ValueError("Video topilmadi. Iltimos, video fayl yuboring.")

    file_id, size, duration, filename = info
    if size and size > MAX_UPLOAD_BYTES:
        raise ValueError(
            "Video 20 MB dan katta. Telegram cloud Bot API cheklovi sabab kichikroq video yuboring."
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
        stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            stderr.decode("utf-8", "ignore")[-1200:] or "FFmpeg xatoligi"
        )


async def make_round(src: Path, dst: Path):
    await run_ffmpeg([
        "-i", str(src),
        "-t", "60",
        "-vf",
        "scale=640:640:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=640:640,setsar=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "0", "-crf", "27",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        "-movflags", "+faststart", str(dst),
    ])


async def make_916(src: Path, dst: Path):
    await run_ffmpeg([
        "-i", str(src),
        "-vf",
        "scale=720:1280:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=720:1280,setsar=1",
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
                        raise ValueError(
                            "Tayyor video juda katta chiqdi. Qisqaroq video yuboring."
                        )
                    await message.answer_video_note(FSInputFile(dst))
                else:
                    await make_916(src, dst)
                    if dst.stat().st_size > MAX_OUTPUT_BYTES:
                        raise ValueError(
                            "Tayyor video 49 MB dan katta. Qisqaroq video yuboring."
                        )
                    await message.answer_video(
                        FSInputFile(dst),
                        caption="📱 9:16 format tayyor!"
                    )

        await status.delete()
    except Exception as exc:
        log.exception("Video processing failed")
        await status.edit_text(
            f"❌ Xatolik: {html.escape(str(exc)[:700])}"
        )
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
    platform: str | None = None
) -> Path:
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError(
            "yt-dlp o'rnatilmagan. requirements.txt faylini tekshiring."
        )

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


# ---------------------------------------------------------------------
# TEXT / VIDEO ROUTERS
# ---------------------------------------------------------------------
@router.message(F.text)
async def handle_text(message: Message):
    text = (message.text or "").strip()
    mode = user_modes.get(message.from_user.id)

    if mode == "ai_image":
        await deliver_ai_image(message, text)
        return

    if mode == "ai_video":
        await deliver_ai_video(message, text)
        return

    if mode == "ai_i2v_prompt":
        image_data = pending_ai_images.get(message.from_user.id)
        if not image_data:
            user_modes[message.from_user.id] = "ai_i2v_image"
            await message.answer("Avval rasm yuboring.")
            return
        raw_image, mime_type = image_data
        start_frame = {
            "type": "inline_base64",
            "content_base64": base64.b64encode(raw_image).decode("ascii"),
            "mime_type": mime_type,
        }
        await deliver_ai_video(message, text, start_frame)
        return

    if mode and mode.startswith("tts:"):
        await process_tts(message, text, mode)
        return

    if mode == "voice_choose":
        await message.answer(
            "Avval ro'yxatdan AI ovozni tugma orqali tanlang."
        )
        return

    if mode == "link":
        if not valid_video_url(text):
            await message.answer(
                "❌ Faqat HTTPS YouTube, Instagram yoki TikTok video havolasini yuboring."
            )
            return

        status = await message.answer(
            "⚡ Video tezkor rejimda yuklanmoqda..."
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
                        caption=f"✅ {platform_title} videosi MP4 formatda tayyor!"
                    )
            await status.delete()
        except Exception:
            log.exception("Link download failed")
            await status.edit_text(
                "❌ Videoni olib bo'lmadi.\n"
                "Sabab: havola yopiq bo'lishi, platforma cheklovi yoki video hajmi bo'lishi mumkin.\n"
                "O'zingizga tegishli yoki yuklashga ruxsatingiz bor ochiq videoni sinab ko'ring."
            )
        finally:
            user_modes.pop(message.from_user.id, None)
        return

    if mode in ("round", "916"):
        await message.answer(
            "Iltimos, havola emas, galereyadan video fayl yuboring."
        )
        return

    if valid_video_url(text):
        await message.answer(
            "Avval 🔗 Video havolasi tugmasini bosing, keyin havolani yuboring."
        )
    else:
        await message.answer(
            "Menyudan kerakli funksiyani tanlang.",
            reply_markup=keyboard
        )


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
    await message.answer(
        "Menyudan kerakli funksiyani tanlang.",
        reply_markup=keyboard
    )


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
    app.router.add_get(
        "/",
        lambda request: web.Response(text="ASAVED bot is running")
    )
    app.router.add_get(
        "/health",
        lambda request: web.Response(text="ok")
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
