import asyncio
import html
import logging
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Awaitable, Callable

from aiohttp import web, ClientSession, ClientTimeout
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application


# ---------------------------------------------------------------------------
# LOGGING / ENVIRONMENT
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("asaved")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook").strip("/")
PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

CHANNEL_USERNAME = "@ReklamaBozoriUzz"
CHANNEL_URL = "https://t.me/ReklamaBozoriUzz"

# Render Persistent Disk ishlatilsa DB_PATH=/var/data/asaved_users.db qiling.
DB_PATH = os.getenv("DB_PATH", "asaved_users.db")

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


# ---------------------------------------------------------------------------
# BOT / ROUTER
# ---------------------------------------------------------------------------

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)


# ---------------------------------------------------------------------------
# BUTTONS / KEYBOARDS
# ---------------------------------------------------------------------------

BTN_ROUND = "🎥 Dumaloq video"
BTN_LINK = "🔗 Video havolasi"
BTN_916 = "📱 9:16 format"
BTN_TTS = "🎙️ Matnni ovozga aylantirish"
BTN_MUSIC = "🎵 AI Music"
BTN_MALE = "👨 Erkak ovozi"
BTN_FEMALE = "👩 Ayol ovozi"
BTN_BACK = "⬅️ Asosiy menyu"

ADMIN_BROADCAST_BTN = "📢 Reklamani hammaga yuborish"

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_ROUND), KeyboardButton(text=BTN_916)],
        [KeyboardButton(text=BTN_LINK)],
        [KeyboardButton(text=BTN_TTS), KeyboardButton(text=BTN_MUSIC)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Kerakli funksiyani tanlang"
)

voice_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_MALE), KeyboardButton(text=BTN_FEMALE)],
        [KeyboardButton(text=BTN_BACK)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Ovoz turini tanlang"
)

subscribe_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📢 Kanalga obuna bo'lish", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_subscription")],
    ]
)


# ---------------------------------------------------------------------------
# SETTINGS / LIMITS
# ---------------------------------------------------------------------------

user_modes: dict[int, str] = {}
subscription_cache: dict[int, float] = {}

SUBSCRIPTION_CACHE_SECONDS = 30
VIDEO_SEMAPHORE = asyncio.Semaphore(2)
TTS_SEMAPHORE = asyncio.Semaphore(2)
MUSIC_SEMAPHORE = asyncio.Semaphore(1)

MAX_TTS_CHARS = 3000
MAX_MUSIC_CHARS = 500
MUSIC_DURATION_SECONDS = 12
MUSIC_TIMEOUT_SECONDS = 900

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
TTS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2").strip()
TTS_VOICES = {
    "male": os.getenv("ELEVENLABS_MALE_VOICE_ID", "pNInz6obpgDQGcFmaJgB").strip(),
    "female": os.getenv("ELEVENLABS_FEMALE_VOICE_ID", "21m00Tcm4TlvDq8ikWAM").strip(),
}

# Hugging Face token ixtiyoriy. Render Environment: HF_TOKEN=hf_...
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
MUSICGEN_SPACE = os.getenv("MUSICGEN_SPACE", "facebook/MusicGen").strip()

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
}

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 49 * 1024 * 1024
MAX_LINK_BYTES = 49 * 1024 * 1024
MAX_SECONDS = 600

_admin_ids_raw = os.getenv("ADMIN_IDS", "") or os.getenv("ADMIN_ID", "")
ADMIN_IDS = {
    int(x.strip())
    for x in _admin_ids_raw.replace(";", ",").split(",")
    if x.strip().isdigit()
}


# ---------------------------------------------------------------------------
# ADVERTISEMENT / USER DATABASE
# ---------------------------------------------------------------------------

AD_TEXT = (
    "🚀 <b>KANALINGIZNI KO'PROQ INSONLARGA TANITING!</b>\n\n"
    "Telegram kanalingiz rivojlanishini, yangi obunachilar e'tiboriga tushishini "
    "xohlaysizmi? Unda ASAVED orqali reklama imkoniyatidan foydalaning!\n\n"
    "📣 <b>Biz nima taklif qilamiz?</b>\n"
    "• Telegram kanalingizni ASAVED bot foydalanuvchilariga tanitish\n"
    "• Loyihangiz haqida ko'proq insonlarga ma'lumot yetkazish\n"
    "• Reklama bo'yicha siz bilan kelishilgan holda ishlash\n\n"
    "✨ <b>Reklama joylashtirish uchun hoziroq bog'laning!</b>\n\n"
    "👤 Admin: @akramovx7\n"
    '<a href="https://t.me/akramovx7">💬 Murojaat qilish</a>'
)


def init_user_db():
    db_file = Path(DB_PATH)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ad_sent_users (
                user_id INTEGER PRIMARY KEY,
                sent_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id INTEGER PRIMARY KEY,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO bot_users (user_id)
            SELECT user_id FROM ad_sent_users
        """)
        conn.commit()


def register_user(user_id: int):
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("""
            INSERT INTO bot_users (user_id) VALUES (?)
            ON CONFLICT(user_id) DO UPDATE SET last_seen=CURRENT_TIMESTAMP
        """, (user_id,))
        conn.commit()


def get_all_user_ids() -> list[int]:
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        rows = conn.execute("SELECT user_id FROM bot_users").fetchall()
    return [int(row[0]) for row in rows]


def mark_ad_sent_once(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO ad_sent_users (user_id) VALUES (?)",
            (user_id,)
        )
        conn.commit()
        return cursor.rowcount == 1


async def send_ad_once(chat_id: int, user_id: int):
    try:
        if not mark_ad_sent_once(user_id):
            return
        await bot.send_message(chat_id, AD_TEXT, disable_web_page_preview=True)
        log.info("Reklama xabari yuborildi: user_id=%s", user_id)
    except Exception:
        log.exception("Reklama xabarini yuborishda xatolik: user_id=%s", user_id)
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.execute("DELETE FROM ad_sent_users WHERE user_id = ?", (user_id,))
            conn.commit()


# ---------------------------------------------------------------------------
# COMMON HELPERS
# ---------------------------------------------------------------------------

def admin_reply_keyboard() -> ReplyKeyboardMarkup:
    rows = [list(row) for row in keyboard.keyboard]
    rows.append([KeyboardButton(text=ADMIN_BROADCAST_BTN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def main_text() -> str:
    return (
        "Assalomu alaykum! <b>ASAVED</b> botiga xush kelibsiz.\n\n"
        "🎥 <b>Dumaloq video</b> — galereyadan 60 soniyagacha video yuboring.\n"
        "📱 <b>9:16 format</b> — galereyadan video yuboring.\n"
        "🔗 <b>Video havolasi</b> — YouTube, Instagram yoki TikTok havolasini yuboring.\n"
        "🎙️ <b>Matnni ovozga aylantirish</b> — erkak yoki ayol AI ovozida MP3 qiling.\n"
        "🎵 <b>AI Music</b> — matn tavsifidan instrumental musiqa yarating.\n\n"
        "Eslatma: AI Music xizmatining bepul limiti va navbati Hugging Face tomonidan belgilanadi."
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


# ---------------------------------------------------------------------------
# SUBSCRIPTION MIDDLEWARE
# ---------------------------------------------------------------------------

class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if not event.from_user:
            return await handler(event, data)

        register_user(event.from_user.id)

        if event.text and event.text.split()[0].split("@")[0] == "/myid":
            return await handler(event, data)

        if event.from_user.id in ADMIN_IDS:
            return await handler(event, data)

        if await is_subscribed(event.from_user.id):
            await send_ad_once(event.chat.id, event.from_user.id)
            return await handler(event, data)

        await send_subscription_required(event)
        return None


router.message.middleware(SubscriptionMiddleware())


# ---------------------------------------------------------------------------
# SUBSCRIPTION CALLBACK
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: CallbackQuery):
    if not callback.from_user:
        await callback.answer()
        return

    register_user(callback.from_user.id)

    if (
        callback.from_user.id in ADMIN_IDS
        or await is_subscribed(callback.from_user.id, use_cache=False)
    ):
        await callback.answer("✅ Obuna tasdiqlandi!")
        if callback.message:
            await send_ad_once(callback.message.chat.id, callback.from_user.id)
            reply_markup = (
                admin_reply_keyboard()
                if callback.from_user.id in ADMIN_IDS
                else keyboard
            )
            await callback.message.answer(main_text(), reply_markup=reply_markup)
    else:
        await callback.answer(
            "❌ Hali kanalga obuna bo'lmagansiz. Avval obuna bo'ling!",
            show_alert=True,
        )


# ---------------------------------------------------------------------------
# ADMIN COMMANDS / BROADCAST
# ---------------------------------------------------------------------------

@router.message(Command("myid"))
async def show_my_id(message: Message):
    if message.from_user:
        await message.answer(
            f"🆔 Sizning Telegram ID raqamingiz: <code>{message.from_user.id}</code>\n\n"
            "Shu raqamni Render → Environment → ADMIN_IDS qiymatiga kiriting."
        )


@router.message(Command("admin"))
async def admin_panel(message: Message):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Sizda admin huquqi yo'q.")
        return

    await message.answer(
        "🛠 <b>Admin panel</b>\n\n"
        f"👥 Bazadagi foydalanuvchilar: <b>{len(get_all_user_ids())}</b>\n"
        "Reklamani hammaga yuborish uchun quyidagi tugmani bosing.",
        reply_markup=admin_reply_keyboard()
    )


@router.message(F.text == ADMIN_BROADCAST_BTN)
async def admin_broadcast_start(message: Message):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return

    total = len(get_all_user_ids())
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Ha, hammaga yuborish",
            callback_data="admin_broadcast_confirm"
        )],
        [InlineKeyboardButton(
            text="❌ Bekor qilish",
            callback_data="admin_broadcast_cancel"
        )],
    ])

    await message.answer(
        f"📢 Reklama <b>{total}</b> ta saqlangan foydalanuvchiga yuboriladi.\n"
        "Har bir tasdiqlashda reklama yana qayta yuboriladi. Davom etasizmi?",
        reply_markup=confirm_kb
    )


@router.callback_query(F.data == "admin_broadcast_cancel")
async def admin_broadcast_cancel(callback: CallbackQuery):
    if not callback.from_user or callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return

    await callback.answer("Bekor qilindi.")
    if callback.message:
        await callback.message.edit_text("❌ Reklama yuborish bekor qilindi.")


@router.callback_query(F.data == "admin_broadcast_confirm")
async def admin_broadcast_confirm(callback: CallbackQuery):
    if not callback.from_user or callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Ruxsat yo'q.", show_alert=True)
        return

    await callback.answer("Yuborish boshlandi.")
    if callback.message:
        await callback.message.edit_text("⏳ Reklama foydalanuvchilarga yuborilmoqda...")

    user_ids = get_all_user_ids()
    sent = 0
    failed = 0

    for user_id in user_ids:
        try:
            await bot.send_message(user_id, AD_TEXT, disable_web_page_preview=True)
            sent += 1
        except Exception as exc:
            failed += 1
            log.info("Broadcast yuborilmadi user_id=%s: %s", user_id, exc)
        await asyncio.sleep(0.05)

    report = (
        "✅ <b>Reklama yuborish yakunlandi!</b>\n\n"
        f"👥 Jami ro'yxatda: <b>{len(user_ids)}</b>\n"
        f"✅ Muvaffaqiyatli: <b>{sent}</b>\n"
        f"❌ Yuborilmadi: <b>{failed}</b>"
    )
    if callback.message:
        await callback.message.answer(report, reply_markup=admin_reply_keyboard())


# ---------------------------------------------------------------------------
# START / MENU HANDLERS
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def start(message: Message):
    if message.from_user:
        user_modes.pop(message.from_user.id, None)

    reply_markup = (
        admin_reply_keyboard()
        if message.from_user and message.from_user.id in ADMIN_IDS
        else keyboard
    )
    await message.answer(main_text(), reply_markup=reply_markup)


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


@router.message(F.text == BTN_TTS)
async def choose_tts(message: Message):
    user_modes[message.from_user.id] = "voice_choose"
    await message.answer(
        "🎙️ Qaysi ElevenLabs ovozida matnni MP3 qilamiz?\n\n"
        "Bu tayyor AI ovozlari; ovoz namunasi yoki voice-cloning ishlatilmaydi.",
        reply_markup=voice_keyboard
    )


@router.message(F.text == BTN_MALE)
async def choose_tts_male(message: Message):
    user_modes[message.from_user.id] = "tts_male"
    reply_markup = (
        admin_reply_keyboard()
        if message.from_user.id in ADMIN_IDS
        else keyboard
    )
    await message.answer(
        "👨 Erkak ovozi tanlandi. Endi ovozga aylantiriladigan matnni yuboring.",
        reply_markup=reply_markup
    )


@router.message(F.text == BTN_FEMALE)
async def choose_tts_female(message: Message):
    user_modes[message.from_user.id] = "tts_female"
    reply_markup = (
        admin_reply_keyboard()
        if message.from_user.id in ADMIN_IDS
        else keyboard
    )
    await message.answer(
        "👩 Ayol ovozi tanlandi. Endi ovozga aylantiriladigan matnni yuboring.",
        reply_markup=reply_markup
    )


@router.message(F.text == BTN_MUSIC)
async def choose_music(message: Message):
    user_modes[message.from_user.id] = "music"
    await message.answer(
        "🎵 <b>AI Music</b>\n\n"
        "Qanday instrumental musiqa kerakligini yozing.\n\n"
        "Misollar:\n"
        "• Sokkin pianino va yomg'ir tovushi\n"
        "• Kuchli kino treyleri musiqasi\n"
        "• Romantik gitara va yumshoq pianino\n"
        "• Zamonaviy rep uchun energiyali beat\n"
        "• Vlog uchun quvnoq elektron musiqa\n\n"
        f"📝 Tavsif {MAX_MUSIC_CHARS} ta belgigacha bo'lsin.\n"
        "Hozircha instrumental musiqa yaratiladi, qo'shiq kuylaydigan odam ovozi emas."
    )


@router.message(F.text == BTN_BACK)
async def back_to_menu(message: Message):
    user_modes.pop(message.from_user.id, None)
    reply_markup = (
        admin_reply_keyboard()
        if message.from_user.id in ADMIN_IDS
        else keyboard
    )
    await message.answer(main_text(), reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# ELEVENLABS TEXT-TO-SPEECH
# ---------------------------------------------------------------------------

async def text_to_speech(text: str, voice_id: str, destination: Path):
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
                    "ElevenLabs API xatosi: status=%s, body=%s",
                    response.status,
                    detail
                )
                if response.status == 401:
                    raise RuntimeError("ElevenLabs API kaliti noto'g'ri yoki faol emas")
                if response.status == 429:
                    raise RuntimeError("ElevenLabs limiti yoki kreditlari tugagan bo'lishi mumkin")
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

    voice_key = "male" if mode == "tts_male" else "female"
    voice = TTS_VOICES[voice_key]
    status = await message.answer("🎙️ Matn ovozga aylantirilmoqda...")

    try:
        async with TTS_SEMAPHORE:
            with tempfile.TemporaryDirectory(prefix="asaved_tts_") as tmp:
                audio_path = Path(tmp) / "asaved_voice.mp3"
                await text_to_speech(text, voice, audio_path)

                if not audio_path.exists() or audio_path.stat().st_size == 0:
                    raise RuntimeError("Audio fayl yaratilmadi")

                await message.answer_audio(
                    FSInputFile(audio_path),
                    title="ASAVED AI ovoz",
                    caption=(
                        "✅ Tayyor! "
                        f"{'Erkak' if voice_key == 'male' else 'Ayol'} AI ovozi"
                    )
                )

        await status.delete()

    except Exception as exc:
        log.exception("TTS generation failed")
        safe_error = html.escape(str(exc)[:350])
        await status.edit_text(f"❌ Ovoz yaratishda xatolik: {safe_error}")

    finally:
        user_modes.pop(message.from_user.id, None)


# ---------------------------------------------------------------------------
# HUGGING FACE MUSICGEN
# ---------------------------------------------------------------------------

def _find_result_file(value: Any) -> Path | None:
    """
    Gradio javobidan lokal audio/video fayl yo'lini topadi.
    Space video waveform qaytarsa, undan FFmpeg audio ajratadi.
    """
    if isinstance(value, (str, Path)):
        candidate = Path(str(value))
        if candidate.is_file():
            return candidate
        return None

    if isinstance(value, dict):
        # Gradio FileData yoki VideoData ko'rinishlari.
        for key in ("path", "video", "audio", "name"):
            found = _find_result_file(value.get(key))
            if found:
                return found
        return None

    if isinstance(value, (list, tuple)):
        for item in value:
            found = _find_result_file(item)
            if found:
                return found

    return None


def generate_music_sync(prompt: str, output_dir: Path) -> Path:
    """
    Facebook MusicGen Hugging Face Space API'siga murojaat qiladi.
    Rasmiy Space hozir batch rejimida 12-15 soniyalik audio beradi.
    """
    try:
        from gradio_client import Client
    except ImportError as exc:
        raise RuntimeError(
            "gradio_client o'rnatilmagan. requirements.txt ga qo'shing."
        ) from exc

    client_kwargs: dict[str, Any] = {"verbose": False}
    if HF_TOKEN:
        client_kwargs["token"] = HF_TOKEN

    client = Client(MUSICGEN_SPACE, **client_kwargs)

    # Facebook MusicGen Space batch endpointi: texts va melodies ro'yxatlari.
    result = client.predict(
        [prompt],
        [None],
        api_name="/predict_batched"
    )

    source_file = _find_result_file(result)
    if not source_file:
        raise RuntimeError(
            "MusicGen javobida audio/video fayl topilmadi. "
            "Space API endpointi o'zgargan bo'lishi mumkin."
        )

    # Gradio Space odatda waveform videosini qaytaradi.
    # Ba'zi versiyalar to'g'ridan-to'g'ri WAV qaytarishi mumkin.
    suffix = source_file.suffix.lower()
    if suffix in {".wav", ".mp3", ".m4a", ".ogg", ".flac"}:
        destination = output_dir / "asaved_music_source" / source_file.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source_file.read_bytes())
        return destination

    return source_file


async def process_music(message: Message, prompt: str):
    prompt = prompt.strip()

    if not prompt:
        await message.answer("🎵 Musiqa qanday bo'lishini yozib yuboring.")
        return

    if len(prompt) > MAX_MUSIC_CHARS:
        await message.answer(
            f"❌ Tavsif juda uzun. {MAX_MUSIC_CHARS} ta belgigacha yozing."
        )
        return

    status = await message.answer(
        "🎵 <b>AI musiqa yaratilmoqda...</b>\n"
        "Hugging Face navbatiga qarab bu bir necha daqiqa olishi mumkin."
    )

    try:
        async with MUSIC_SEMAPHORE:
            with tempfile.TemporaryDirectory(prefix="asaved_music_") as tmp:
                folder = Path(tmp)
                source = await asyncio.wait_for(
                    asyncio.to_thread(generate_music_sync, prompt, folder),
                    timeout=MUSIC_TIMEOUT_SECONDS
                )

                mp3_path = folder / "asaved_music.mp3"

                await run_ffmpeg([
                    "-i", str(source),
                    "-vn",
                    "-codec:a", "libmp3lame",
                    "-q:a", "3",
                    "-y",
                    str(mp3_path)
                ])

                if not mp3_path.exists() or mp3_path.stat().st_size == 0:
                    raise RuntimeError("MP3 audio fayli yaratilmadi")

                if mp3_path.stat().st_size > MAX_OUTPUT_BYTES:
                    raise RuntimeError("Tayyor musiqa fayli Telegram limitidan katta.")

                await message.answer_audio(
                    FSInputFile(mp3_path),
                    title="ASAVED AI Music",
                    caption="🎵 Musiqangiz tayyor!"
                )

        await status.delete()

    except asyncio.TimeoutError:
        await status.edit_text(
            "⌛ Musiqa yaratish vaqti tugadi. Hugging Face navbati band bo'lishi mumkin. "
            "Keyinroq qayta urinib ko'ring."
        )
    except Exception as exc:
        log.exception("MusicGen generation failed")
        error_text = str(exc).lower()

        if "quota" in error_text or "zerogpu" in error_text or "rate limit" in error_text:
            user_error = (
                "Hugging Face bepul GPU limiti tugagan yoki navbat band. "
                "Keyinroq qayta urinib ko'ring."
            )
        elif "not found" in error_text or "api_name" in error_text:
            user_error = (
                "MusicGen Space API manzili o'zgargan bo'lishi mumkin. "
                "Admin Space API'sini tekshirishi kerak."
            )
        else:
            user_error = "Musiqa yaratishda xatolik bo'ldi. Birozdan keyin qayta urinib ko'ring."

        await status.edit_text(f"❌ {user_error}")

    finally:
        user_modes.pop(message.from_user.id, None)


# ---------------------------------------------------------------------------
# VIDEO HELPERS
# ---------------------------------------------------------------------------

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
        "scale=640:640:force_original_aspect_ratio=increase:flags=fast_bilinear,"
        "crop=640:640,setsar=1",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-threads", "0",
        "-crf", "27",
        "-c:a", "aac",
        "-b:a", "96k",
        "-ac", "2",
        "-movflags", "+faststart",
        str(dst)
    ])


async def make_916(src: Path, dst: Path):
    await run_ffmpeg([
        "-i", str(src),
        "-vf",
        "scale=720:1280:force_original_aspect_ratio=increase:flags=fast_bilinear,"
        "crop=720:1280,setsar=1",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-threads", "0",
        "-crf", "26",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(dst)
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
        await status.edit_text(f"❌ Xatolik: {html.escape(str(exc)[:700])}")

    finally:
        user_modes.pop(message.from_user.id, None)


# ---------------------------------------------------------------------------
# VIDEO LINK DOWNLOAD
# ---------------------------------------------------------------------------

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
    except ImportError as exc:
        raise RuntimeError(
            "yt-dlp o'rnatilmagan. requirements.txt faylini tekshiring."
        ) from exc

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


# ---------------------------------------------------------------------------
# TEXT MESSAGE HANDLER
# ---------------------------------------------------------------------------

@router.message(F.text)
async def handle_text(message: Message):
    if not message.from_user:
        return

    text = (message.text or "").strip()
    mode = user_modes.get(message.from_user.id)

    if mode in ("tts_male", "tts_female"):
        await process_tts(message, text, mode)
        return

    if mode == "music":
        await process_music(message, text)
        return

    if mode == "voice_choose":
        await message.answer(
            "Avval 👨 Erkak ovozi yoki 👩 Ayol ovozini tanlang.",
            reply_markup=voice_keyboard
        )
        return

    if mode == "link":
        if not valid_video_url(text):
            await message.answer(
                "❌ Faqat HTTPS YouTube, Instagram yoki TikTok video havolasini yuboring."
            )
            return

        status = await message.answer(
            "⚡ Video yuklanmoqda... Platforma va navbatga qarab vaqt olishi mumkin."
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
        reply_markup = (
            admin_reply_keyboard()
            if message.from_user.id in ADMIN_IDS
            else keyboard
        )
        await message.answer(
            "Menyudan kerakli funksiyani tanlang.",
            reply_markup=reply_markup
        )


# ---------------------------------------------------------------------------
# VIDEO MESSAGE HANDLER
# ---------------------------------------------------------------------------

@router.message(F.video | F.document)
async def handle_video(message: Message):
    if not message.from_user:
        return

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


# ---------------------------------------------------------------------------
# FALLBACK
# ---------------------------------------------------------------------------

@router.message()
async def fallback(message: Message):
    if not message.from_user:
        return

    reply_markup = (
        admin_reply_keyboard()
        if message.from_user.id in ADMIN_IDS
        else keyboard
    )
    await message.answer(
        "Menyudan kerakli funksiyani tanlang.",
        reply_markup=reply_markup
    )


# ---------------------------------------------------------------------------
# WEBHOOK STARTUP / SHUTDOWN
# ---------------------------------------------------------------------------

async def on_startup():
    init_user_db()
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
    init_user_db()

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
        secret_token=WEBHOOK_SECRET
    ).register(app, path=f"/{WEBHOOK_PATH}")

    setup_application(app, dp, bot=bot)

    async def startup_callback(app_instance):
        await on_startup()

    async def cleanup_callback(app_instance):
        await on_shutdown()

    app.on_startup.append(startup_callback)
    app.on_cleanup.append(cleanup_callback)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    log.info("ASAVED listening on port %s", PORT)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
