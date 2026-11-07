import asyncio
import html
import logging
import os
import re
import secrets
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from aiohttp import web, ClientSession, ClientTimeout
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, WebAppInfo,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from contest import setup_contest
from admin_panel import setup_admin_panel, get_channels, is_blocked

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("asaved")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook").strip("/")
PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "asaved_users.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN Environment Variable kiritilmagan")
if not WEBHOOK_SECRET or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET kamida 16 ta belgi bo‘lsin")
if not BASE_URL.startswith("https://"):
    raise RuntimeError("RENDER_EXTERNAL_URL HTTPS manzil bo‘lishi kerak")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

BTN_ROUND = "🎥 Dumaloq video"
BTN_LINK = "🔗 Video havolasi"
BTN_916 = "📱 9:16 format"
BTN_TTS = "🎙️ Matnni ovozga aylantirish"
BTN_MALE = "👨 Erkak ovozi"
BTN_FEMALE = "👩 Ayol ovozi"
ADMIN_BROADCAST_BTN = "📢 Reklamani hammaga yuborish"

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_ROUND), KeyboardButton(text=BTN_916)],
        [KeyboardButton(text=BTN_LINK)],
        [KeyboardButton(text=BTN_TTS)],
    ], resize_keyboard=True, input_field_placeholder="Kerakli funksiyani tanlang"
)
voice_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_MALE), KeyboardButton(text=BTN_FEMALE)],
        [KeyboardButton(text="⬅️ Asosiy menyu")],
    ], resize_keyboard=True, input_field_placeholder="Ovoz turini tanlang"
)

_ad_ids = os.getenv("ADMIN_IDS", "") or os.getenv("ADMIN_ID", "")
ADMIN_IDS = {
    int(x.strip()) for x in _ad_ids.replace(";", ",").split(",")
    if x.strip().isdigit()
}

AD_TEXT_DEFAULT = (
    "🚀 <b>KANALINGIZNI KO‘PROQ INSONLARGA TANITING!</b>\n\n"
    "Telegram kanalingiz rivojlanishini xohlaysizmi? ASAVED orqali reklama imkoniyatidan foydalaning!\n\n"
    "📣 Kanalingizni ASAVED foydalanuvchilariga tanitamiz.\n\n"
    "👤 Admin: @akramovx7\n"
    '<a href="https://t.me/akramovx7">Murojaat qilish</a>'
)
WELCOME_DEFAULT = (
    "Assalomu alaykum! <b>ASAVED</b> botiga xush kelibsiz.\n\n"
    "🎥 <b>Dumaloq video</b> — galereyadan 60 soniyagacha video yuboring.\n"
    "📱 <b>9:16 format</b> — galereyadan video yuboring.\n"
    "🔗 <b>Video havolasi</b> — YouTube, Instagram yoki TikTok havolasini yuboring.\n"
    "🎙️ <b>Matnni ovozga aylantirish</b> — ElevenLabs tayyor AI ovozida MP3 qiling.\n\n"
    "Faqat o‘zingizga tegishli yoki yuklab olishga ruxsatingiz bor videolardan foydalaning."
)

contest_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(
        text="🎁 ASAVED KONKURSDA QATNASHISH",
        web_app=WebAppInfo(url=f"{BASE_URL}/contest"),
    )
]])

user_modes: dict[int, str] = {}
subscription_cache: dict[int, float] = {}
SUBSCRIPTION_CACHE_SECONDS = 30
VIDEO_SEMAPHORE = asyncio.Semaphore(2)
TTS_SEMAPHORE = asyncio.Semaphore(2)
MAX_TTS_CHARS = 3000

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
TTS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2").strip()
TTS_VOICES = {
    "male": os.getenv("ELEVENLABS_MALE_VOICE_ID", "pNInz6obpgDQGcFmaJgB").strip(),
    "female": os.getenv("ELEVENLABS_FEMALE_VOICE_ID", "21m00Tcm4TlvDq8ikWAM").strip(),
}

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "instagram.com", "www.instagram.com", "tiktok.com", "www.tiktok.com",
    "vm.tiktok.com", "vt.tiktok.com",
}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 49 * 1024 * 1024
MAX_LINK_BYTES = 49 * 1024 * 1024
MAX_SECONDS = 600


def db_connect():
    return sqlite3.connect(DB_PATH, timeout=30)


def init_user_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db_connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS ad_sent_users (
            user_id INTEGER PRIMARY KEY, sent_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS bot_users (
            user_id INTEGER PRIMARY KEY,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("INSERT OR IGNORE INTO bot_users(user_id) SELECT user_id FROM ad_sent_users")


def register_user(user_id: int):
    with db_connect() as conn:
        conn.execute(
            """INSERT INTO bot_users(user_id) VALUES(?)
            ON CONFLICT(user_id) DO UPDATE SET last_seen=CURRENT_TIMESTAMP""",
            (user_id,),
        )


def get_all_user_ids() -> list[int]:
    with db_connect() as conn:
        return [int(r[0]) for r in conn.execute("SELECT user_id FROM bot_users")]


def mark_ad_sent_once(user_id: int) -> bool:
    with db_connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO ad_sent_users(user_id) VALUES(?)", (user_id,))
        return cur.rowcount == 1


def get_setting(key: str, default: str) -> str:
    try:
        with db_connect() as conn:
            row = conn.execute("SELECT value FROM panel_settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default
    except sqlite3.Error:
        return default


def main_text() -> str:
    return get_setting("welcome_text", WELCOME_DEFAULT)


def ad_text() -> str:
    return get_setting("ad_text", AD_TEXT_DEFAULT)


async def send_ad_once(chat_id: int, user_id: int):
    try:
        if not mark_ad_sent_once(user_id):
            return
        await bot.send_message(chat_id, ad_text(), disable_web_page_preview=True)
    except Exception:
        log.exception("Reklama xabarini yuborishda xatolik: %s", user_id)
        with db_connect() as conn:
            conn.execute("DELETE FROM ad_sent_users WHERE user_id=?", (user_id,))


async def is_subscribed(user_id: int, use_cache: bool = True) -> bool:
    now = asyncio.get_running_loop().time()
    if use_cache and subscription_cache.get(user_id, 0) > now:
        return True
    channels = get_channels(DB_PATH)
    if not channels:
        return True
    for channel in channels:
        try:
            member = await bot.get_chat_member(channel["chat_id"], user_id)
            if member.status not in {
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            }:
                subscription_cache.pop(user_id, None)
                return False
        except Exception:
            log.exception("Kanal obunasini tekshirishda xatolik: %s", channel["chat_id"])
            subscription_cache.pop(user_id, None)
            return False
    subscription_cache[user_id] = now + SUBSCRIPTION_CACHE_SECONDS
    return True


def subscription_markup():
    rows = []
    for channel in get_channels(DB_PATH):
        rows.append([InlineKeyboardButton(text=f"📢 {channel['title']}", url=channel["join_url"])])
    rows.append([InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_subscription")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_subscription_required(message: Message):
    await message.answer(
        "⛔ Botdan foydalanish uchun avval quyidagi kanal(lar)ga obuna bo‘ling.\n"
        "Obuna bo‘lgach, «Obunani tekshirish» tugmasini bosing.",
        reply_markup=subscription_markup(),
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

        uid = event.from_user.id
        register_user(uid)

        if event.text and event.text.split()[0].split("@")[0] == "/myid":
            return await handler(event, data)

        if is_blocked(DB_PATH, uid):
            await event.answer("⛔ Siz admin tomonidan botdan foydalanishdan bloklangansiz.")
            return None

        if uid in ADMIN_IDS or await is_subscribed(uid):
            if uid not in ADMIN_IDS:
                await send_ad_once(event.chat.id, uid)
            return await handler(event, data)

        await send_subscription_required(event)
        return None


router.message.middleware(SubscriptionMiddleware())


def admin_reply_keyboard() -> ReplyKeyboardMarkup:
    rows = [list(row) for row in keyboard.keyboard]
    rows.append([KeyboardButton(text=ADMIN_BROADCAST_BTN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def send_welcome(message: Message):
    is_admin = bool(message.from_user and message.from_user.id in ADMIN_IDS)
    await message.answer(main_text(), reply_markup=admin_reply_keyboard() if is_admin else keyboard)
    await message.answer(
        "🎁 <b>ASAVED KATTA KONKURSI!</b>\n\nStudio mikrofonini yutib olish imkoniyatida qatnashing!",
        reply_markup=contest_keyboard,
    )


@router.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: CallbackQuery):
    if not callback.from_user:
        await callback.answer()
        return
    uid = callback.from_user.id
    register_user(uid)
    if uid in ADMIN_IDS or (not is_blocked(DB_PATH, uid) and await is_subscribed(uid, use_cache=False)):
        subscription_cache.pop(uid, None)
        await callback.answer("✅ Obuna tasdiqlandi!")
        if callback.message:
            if uid not in ADMIN_IDS:
                await send_ad_once(callback.message.chat.id, uid)
            await send_welcome(callback.message)
    else:
        await callback.answer("❌ Hali barcha kanallarga obuna bo‘lmagansiz.", show_alert=True)


@router.message(Command("myid"))
async def show_my_id(message: Message):
    if message.from_user:
        await message.answer(
            f"🆔 Telegram ID: <code>{message.from_user.id}</code>\n"
            "Render → Environment → ADMIN_IDS ga kiriting."
        )


@router.message(Command("admin"))
async def admin_panel_command(message: Message):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Sizda admin huquqi yo‘q.")
        return
    await message.answer(
        f"🛠 <b>Admin panel</b>\n\n👥 Bazadagi foydalanuvchilar: <b>{len(get_all_user_ids())}</b>\n"
        "Web Panel: /admin",
        reply_markup=admin_reply_keyboard(),
    )


@router.message(Command("winner"))
async def choose_winner(message: Message):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Sizda admin huquqi yo‘q.")
        return
    try:
        with sqlite3.connect(os.getenv("CONTEST_DB_PATH", DB_PATH), timeout=30) as conn:
            rows = conn.execute(
                "SELECT user_id, username, first_name FROM contest_entries"
            ).fetchall()
    except sqlite3.Error:
        await message.answer("❌ Konkurs bazasi topilmadi yoki hali qatnashchi yo‘q.")
        return
    if not rows:
        await message.answer("Hozircha konkurs qatnashchilari yo‘q.")
        return
    uid, username, first_name = secrets.choice(rows)
    who = f"@{username}" if username else html.escape(first_name or str(uid))
    await message.answer(
        f"🎉 <b>KONKURS G‘OLIBI</b>\n\n👤 {who}\n🆔 <code>{uid}</code>\n"
        f"👥 Jami qatnashchilar: <b>{len(rows)}</b>"
    )


@router.message(F.text == ADMIN_BROADCAST_BTN)
async def admin_broadcast_start(message: Message):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    total = len(get_all_user_ids())
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ha, hammaga yuborish", callback_data="admin_broadcast_confirm")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin_broadcast_cancel")],
    ])
    await message.answer(
        f"📢 Reklama <b>{total}</b> ta saqlangan foydalanuvchiga yuboriladi. Davom etasizmi?",
        reply_markup=kb,
    )


@router.callback_query(F.data == "admin_broadcast_cancel")
async def admin_broadcast_cancel(callback: CallbackQuery):
    if not callback.from_user or callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Ruxsat yo‘q.", show_alert=True)
        return
    await callback.answer("Bekor qilindi.")
    if callback.message:
        await callback.message.edit_text("❌ Reklama yuborish bekor qilindi.")


@router.callback_query(F.data == "admin_broadcast_confirm")
async def admin_broadcast_confirm(callback: CallbackQuery):
    if not callback.from_user or callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Ruxsat yo‘q.", show_alert=True)
        return
    await callback.answer("Yuborish boshlandi.")
    if callback.message:
        await callback.message.edit_text("⏳ Reklama foydalanuvchilarga yuborilmoqda...")
    ids = get_all_user_ids()
    sent = failed = 0
    for uid in ids:
        try:
            if not is_blocked(DB_PATH, uid):
                await bot.send_message(uid, ad_text(), disable_web_page_preview=True)
                sent += 1
        except Exception as exc:
            failed += 1
            log.info("Broadcast yuborilmadi user_id=%s: %s", uid, exc)
        await asyncio.sleep(0.05)
    if callback.message:
        await callback.message.answer(
            f"✅ <b>Yakunlandi!</b>\n\n👥 Jami: <b>{len(ids)}</b>\n"
            f"✅ Yuborildi: <b>{sent}</b>\n❌ Yuborilmadi: <b>{failed}</b>",
            reply_markup=admin_reply_keyboard(),
        )


@router.message(CommandStart())
async def start(message: Message):
    user_modes.pop(message.from_user.id, None)
    await send_welcome(message)


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


@router.message(F.text == BTN_TTS)
async def choose_tts(message: Message):
    user_modes[message.from_user.id] = "voice_choose"
    await message.answer(
        "🎙️ Qaysi ElevenLabs tayyor ovozida matnni MP3 qilamiz?\n\nVoice-cloning ishlatilmaydi.",
        reply_markup=voice_keyboard,
    )


@router.message(F.text == BTN_MALE)
async def choose_tts_male(message: Message):
    user_modes[message.from_user.id] = "tts_male"
    await message.answer(
        "👨 Erkak ovozi tanlandi. Endi matn yuboring.",
        reply_markup=admin_reply_keyboard() if message.from_user.id in ADMIN_IDS else keyboard,
    )


@router.message(F.text == BTN_FEMALE)
async def choose_tts_female(message: Message):
    user_modes[message.from_user.id] = "tts_female"
    await message.answer(
        "👩 Ayol ovozi tanlandi. Endi matn yuboring.",
        reply_markup=admin_reply_keyboard() if message.from_user.id in ADMIN_IDS else keyboard,
    )


@router.message(F.text == "⬅️ Asosiy menyu")
async def back_to_menu(message: Message):
    user_modes.pop(message.from_user.id, None)
    await send_welcome(message)


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
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    async with ClientSession(timeout=ClientTimeout(total=120, connect=20)) as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                detail = (await response.text())[:300]
                log.error("ElevenLabs xatosi %s: %s", response.status, detail)
                if response.status == 401:
                    raise RuntimeError("ElevenLabs API kaliti noto‘g‘ri")
                if response.status == 429:
                    raise RuntimeError("ElevenLabs limiti yoki kreditlari tugagan bo‘lishi mumkin")
                raise RuntimeError(f"ElevenLabs API xatosi ({response.status})")
            data = await response.read()
    if not data:
        raise RuntimeError("Bo‘sh audio qaytdi")
    destination.write_bytes(data)


async def process_tts(message: Message, text: str, mode: str):
    if not text.strip():
        await message.answer("Ovozga aylantirish uchun matn yuboring.")
        return
    if len(text) > MAX_TTS_CHARS:
        await message.answer(f"Matn juda uzun. Eng ko‘pi {MAX_TTS_CHARS} ta belgi yuboring.")
        return
    key = "male" if mode == "tts_male" else "female"
    status = await message.answer("🎙️ Matn ovozga aylantirilmoqda...")
    try:
        async with TTS_SEMAPHORE:
            with tempfile.TemporaryDirectory(prefix="asaved_tts_") as tmp:
                audio = Path(tmp) / "asaved_voice.mp3"
                await text_to_speech(text, TTS_VOICES[key], audio)
                await message.answer_audio(
                    FSInputFile(audio),
                    title="ASAVED AI ovoz",
                    caption=f"✅ Tayyor! {'Erkak' if key == 'male' else 'Ayol'} AI ovozi",
                )
        await status.delete()
    except Exception:
        log.exception("TTS generation failed")
        await status.edit_text("❌ Ovoz yaratishda xatolik bo‘ldi. Birozdan keyin qayta urinib ko‘ring.")
    finally:
        user_modes.pop(message.from_user.id, None)


def get_video_info(message: Message):
    if message.video:
        return message.video.file_id, message.video.file_size, message.video.duration
    if message.document and (message.document.mime_type or "").startswith("video/"):
        return message.document.file_id, message.document.file_size, None
    return None


async def download_telegram_video(message: Message, target: Path):
    info = get_video_info(message)
    if not info:
        raise ValueError("Video topilmadi. Video fayl yuboring.")
    file_id, size, duration = info
    if size and size > MAX_UPLOAD_BYTES:
        raise ValueError("Video 20 MB dan katta. Kichikroq video yuboring.")
    if duration and duration > MAX_SECONDS:
        raise ValueError("Video 10 daqiqadan uzun. Qisqaroq video yuboring.")
    tg_file = await bot.get_file(file_id)
    await bot.download_file(tg_file.file_path, destination=target)
    return target


async def run_ffmpeg(args: list[str]):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(err.decode("utf-8", "ignore")[-1000:] or "FFmpeg xatoligi")


async def make_round(src: Path, dst: Path):
    await run_ffmpeg([
        "-i", str(src), "-t", "60",
        "-vf", "scale=640:640:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=640:640,setsar=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "0", "-crf", "27",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-movflags", "+faststart", str(dst),
    ])


async def make_916(src: Path, dst: Path):
    await run_ffmpeg([
        "-i", str(src),
        "-vf", "scale=720:1280:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=720:1280,setsar=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "0", "-crf", "26",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst),
    ])


async def process_uploaded_video(message: Message, mode: str):
    status = await message.answer("⚡ Video tayyorlanmoqda...")
    try:
        async with VIDEO_SEMAPHORE:
            with tempfile.TemporaryDirectory(prefix="asaved_") as tmp:
                src, dst = Path(tmp) / "input_video", Path(tmp) / "output.mp4"
                await download_telegram_video(message, src)
                if mode == "round":
                    await make_round(src, dst)
                else:
                    await make_916(src, dst)
                if dst.stat().st_size > MAX_OUTPUT_BYTES:
                    raise ValueError("Tayyor video 49 MB dan katta. Qisqaroq video yuboring.")
                if mode == "round":
                    await message.answer_video_note(FSInputFile(dst))
                else:
                    await message.answer_video(FSInputFile(dst), caption="📱 9:16 format tayyor!")
        await status.delete()
    except Exception as exc:
        log.exception("Video processing failed")
        await status.edit_text(f"❌ Xatolik: {html.escape(str(exc)[:600])}")
    finally:
        user_modes.pop(message.from_user.id, None)


def valid_video_url(raw: str) -> bool:
    try:
        p = urlparse(raw.strip())
        host = (p.hostname or "").lower()
        return p.scheme == "https" and (
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


async def download_public_link(url: str, folder: Path) -> Path:
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp o‘rnatilmagan. requirements.txt ni tekshiring.")
    opts = {
        "format": "bestvideo+bestaudio/best",
        "format_sort": ["res", "quality", "fps"],
        "outtmpl": str(folder / "link_video.%(ext)s"),
        "noplaylist": True,
        "max_filesize": MAX_LINK_BYTES,
        "socket_timeout": 15,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "concurrent_fragment_downloads": 4,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "remuxvideo": "mp4",
    }

    def download():
        import yt_dlp
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    await asyncio.to_thread(download)
    files = [p for p in folder.iterdir() if p.is_file() and p.name.startswith("link_video.")]
    if not files:
        raise RuntimeError("Video topilmadi yoki platforma yuklashga ruxsat bermadi.")
    result = max(files, key=lambda p: p.stat().st_size)
    if result.stat().st_size > MAX_LINK_BYTES:
        raise RuntimeError("Video 49 MB dan katta.")
    return result


@router.message(F.text)
async def handle_text(message: Message):
    text = (message.text or "").strip()
    mode = user_modes.get(message.from_user.id)
    if mode in ("tts_male", "tts_female"):
        await process_tts(message, text, mode)
        return
    if mode == "voice_choose":
        await message.answer("Avval erkak yoki ayol ovozini tanlang.", reply_markup=voice_keyboard)
        return
    if mode == "link":
        if not valid_video_url(text):
            await message.answer("❌ Faqat HTTPS YouTube, Instagram yoki TikTok video havolasini yuboring.")
            return
        status = await message.answer("⚡ Video yuklanmoqda...")
        try:
            _, title = detect_platform(text)
            async with VIDEO_SEMAPHORE:
                with tempfile.TemporaryDirectory(prefix="asaved_link_") as tmp:
                    path = await download_public_link(text, Path(tmp))
                    await message.answer_video(
                        FSInputFile(path),
                        caption=f"✅ {title} videosi MP4 formatda tayyor!",
                    )
            await status.delete()
        except Exception:
            log.exception("Link download failed")
            await status.edit_text(
                "❌ Videoni olib bo‘lmadi. Havola yopiq, platforma cheklagan yoki video juda katta bo‘lishi mumkin."
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
        await message.answer(
            "Menyudan kerakli funksiyani tanlang.",
            reply_markup=admin_reply_keyboard() if message.from_user.id in ADMIN_IDS else keyboard,
        )


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
    await message.answer(
        "Menyudan kerakli funksiyani tanlang.",
        reply_markup=admin_reply_keyboard()
        if message.from_user and message.from_user.id in ADMIN_IDS else keyboard,
    )


async def on_startup():
    init_user_db()
    url = f"{BASE_URL}/{WEBHOOK_PATH}"
    await bot.set_webhook(
        url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types(),
    )
    log.info("Webhook set: %s", url)


async def on_shutdown():
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.session.close()


async def main():
    init_user_db()
    app = web.Application(client_max_size=2 * 1024 * 1024)

    # Konkurs Mini App o‘z holicha saqlanadi; Web Panelda konkurs boshqaruvi yo‘q.
    setup_contest(app, bot, ADMIN_IDS)

    # Admin Web Panel: /admin
    setup_admin_panel(app, bot, DB_PATH, WEBHOOK_SECRET, ADMIN_IDS)

    async def index(_request):
        return web.Response(text="ASAVED bot is running")

    async def health(_request):
        return web.Response(text="ok")

    app.router.add_get("/", index)
    app.router.add_get("/health", health)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    ).register(app, path=f"/{WEBHOOK_PATH}")
    setup_application(app, dp, bot=bot)

    app.on_startup.append(lambda _app: on_startup())
    app.on_cleanup.append(lambda _app: on_shutdown())

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("ASAVED listening on port %s", PORT)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
