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
    Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# =========================================================
# ASAVED — barcha funksiyalar
# =========================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("asaved")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook").strip("/")
PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "asaved_users.db")

CHANNEL_USERNAME = "@kinoolamiuzzzz"
CHANNEL_URL = "https://t.me/kinoolamiuzzzz"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN Environment Variable kiritilmagan")
if not WEBHOOK_SECRET or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET noto‘g‘ri yoki kiritilmagan")
if not BASE_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL topilmadi")

ADMIN_RAW = os.getenv("ADMIN_IDS", "") or os.getenv("ADMIN_ID", "")
ADMIN_IDS = {
    int(x.strip()) for x in ADMIN_RAW.replace(";", ",").split(",")
    if x.strip().isdigit()
}

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Tugmalar
BTN_ROUND = "🎥 Dumaloq video"
BTN_916 = "📱 9:16 format"
BTN_LINK = "🔗 Video havolasi"
BTN_TTS = "🎙️ Matnni ovozga aylantirish"
BTN_MOVIE_SEARCH = "🎬 Kino qidirish"
BTN_MY_VIDEOS = "📁 Mening videolarim"
BTN_ADD_MOVIE = "➕ Kino qo‘shish"
BTN_BROADCAST = "📢 Reklamani hammaga yuborish"
BTN_MALE = "👨 Erkak ovozi"
BTN_FEMALE = "👩 Ayol ovozi"
BTN_MENU = "⬅️ Asosiy menyu"

user_modes: dict[int, str] = {}
movie_uploads: dict[int, dict] = {}
subscription_cache: dict[int, float] = {}
SUB_CACHE_SECONDS = 30

VIDEO_SEMAPHORE = asyncio.Semaphore(2)
TTS_SEMAPHORE = asyncio.Semaphore(2)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 49 * 1024 * 1024
MAX_LINK_BYTES = 49 * 1024 * 1024
MAX_SECONDS = 600
MAX_TTS_CHARS = 3000

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
TTS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2").strip()
TTS_VOICES = {
    "male": os.getenv("ELEVENLABS_MALE_VOICE_ID", "pNInz6obpgDQGcFmaJgB").strip(),
    "female": os.getenv("ELEVENLABS_FEMALE_VOICE_ID", "21m00Tcm4TlvDq8ikWAM").strip(),
}

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
}

AD_TEXT = (
    "🚀 <b>KANALINGIZNI KO‘PROQ INSONLARGA TANITING!</b>\n\n"
    "Telegram kanalingiz rivojlanishini xohlaysizmi?\n"
    "📣 ASAVED orqali reklama joylashtiring!\n\n"
    "👤 Admin: @akramovx7\n"
    '<a href="https://t.me/akramovx7">💬 Murojaat qilish</a>'
)

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_MOVIE_SEARCH)],
        [KeyboardButton(text=BTN_ROUND), KeyboardButton(text=BTN_916)],
        [KeyboardButton(text=BTN_LINK)],
        [KeyboardButton(text=BTN_TTS)],
        [KeyboardButton(text=BTN_MY_VIDEOS)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Kerakli funksiyani tanlang"
)
voice_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_MALE), KeyboardButton(text=BTN_FEMALE)],
        [KeyboardButton(text=BTN_MENU)],
    ],
    resize_keyboard=True
)
subscribe_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🎬 Kino Olami kanaliga obuna bo‘lish", url=CHANNEL_URL)],
    [InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_subscription")],
])

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def admin_keyboard() -> ReplyKeyboardMarkup:
    rows = [list(row) for row in keyboard.keyboard]
    rows += [
        [KeyboardButton(text=BTN_ADD_MOVIE)],
        [KeyboardButton(text=BTN_BROADCAST)],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def menu_for(user_id: int):
    return admin_keyboard() if is_admin(user_id) else keyboard

def main_text() -> str:
    return (
        "Assalomu alaykum! <b>ASAVED</b> botiga xush kelibsiz.\n\n"
        "🎬 Kino qidirish — kino nomi yoki raqamini yuboring.\n"
        "🎥 Dumaloq video — 60 soniyagacha videoni dumaloq qiling.\n"
        "📱 9:16 format — videoni vertikal formatga o‘tkazing.\n"
        "🔗 Video havolasi — YouTube, Instagram yoki TikTok.\n"
        "🎙️ Matnni ovozga aylantirish — matnni MP3 qiling.\n"
        "📁 Mening videolarim — oldin tayyorlangan fayllarni qayta oling."
    )

# ------------------------- DATABASE -------------------------

def db():
    return sqlite3.connect(DB_PATH, timeout=30)

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS bot_users(
            user_id INTEGER PRIMARY KEY,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS ad_sent_users(
            user_id INTEGER PRIMARY KEY,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS movies(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            movie_number INTEGER UNIQUE NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            file_id TEXT NOT NULL,
            media_type TEXT DEFAULT 'video',
            poster_file_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS saved_videos(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            media_type TEXT NOT NULL,
            label TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_saved_user ON saved_videos(user_id, id DESC)")
        conn.commit()

def register_user(user_id: int):
    with db() as conn:
        conn.execute("""INSERT INTO bot_users(user_id) VALUES(?)
            ON CONFLICT(user_id) DO UPDATE SET last_seen=CURRENT_TIMESTAMP""", (user_id,))
        conn.commit()

def all_user_ids() -> list[int]:
    with db() as conn:
        return [int(r[0]) for r in conn.execute("SELECT user_id FROM bot_users").fetchall()]

def save_user_video(user_id: int, file_id: str, media_type: str, label: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO saved_videos(user_id,file_id,media_type,label) VALUES(?,?,?,?)",
            (user_id, file_id, media_type, label[:100])
        )
        conn.commit()

def get_user_videos(user_id: int, limit: int = 15) -> list[tuple]:
    with db() as conn:
        return conn.execute(
            "SELECT id,label,media_type,created_at FROM saved_videos WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()

def get_saved_video(video_id: int, user_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT file_id,media_type,label FROM saved_videos WHERE id=? AND user_id=?",
            (video_id, user_id)
        ).fetchone()
        return row

def save_movie(number, title, description, file_id, media_type, poster_file_id):
    with db() as conn:
        conn.execute("""INSERT INTO movies(movie_number,title,description,file_id,media_type,poster_file_id)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(movie_number) DO UPDATE SET
            title=excluded.title, description=excluded.description, file_id=excluded.file_id,
            media_type=excluded.media_type, poster_file_id=excluded.poster_file_id""",
            (number, title, description, file_id, media_type, poster_file_id))
        conn.commit()

def search_movies(query: str):
    with db() as conn:
        conn.row_factory = sqlite3.Row
        if query.isdigit():
            rows = conn.execute(
                "SELECT id,movie_number,title,description FROM movies WHERE movie_number=? LIMIT 10",
                (int(query),)
            ).fetchall()
            if rows:
                return [tuple(r) for r in rows]
        rows = conn.execute(
            "SELECT id,movie_number,title,description FROM movies WHERE title LIKE ? ORDER BY movie_number LIMIT 10",
            (f"%{query}%",)
        ).fetchall()
        return [tuple(r) for r in rows]

def get_movie(movie_id: int):
    with db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
        return dict(row) if row else None

def delete_movie(number: int) -> bool:
    with db() as conn:
        cur = conn.execute("DELETE FROM movies WHERE movie_number=?", (number,))
        conn.commit()
        return cur.rowcount > 0

async def send_saved_file(chat_id: int, file_id: str, media_type: str, caption: str = ""):
    if media_type == "video_note":
        await bot.send_video_note(chat_id, file_id)
    elif media_type == "audio":
        await bot.send_audio(chat_id, file_id, caption=caption or None)
    elif media_type == "document":
        await bot.send_document(chat_id, file_id, caption=caption or None)
    else:
        await bot.send_video(chat_id, file_id, caption=caption or None, supports_streaming=True)

async def send_movie(chat_id: int, movie: dict):
    caption = f"🎬 <b>{html.escape(movie['title'])}</b>"
    desc = html.escape(movie.get("description") or "")
    if desc:
        caption += "\n\n" + desc
    if movie.get("poster_file_id"):
        await bot.send_photo(chat_id, movie["poster_file_id"], caption=caption[:1024])
        caption = ""
    await send_saved_file(chat_id, movie["file_id"], movie["media_type"], caption[:1024])

# ------------------------- SUBSCRIPTION -------------------------

async def is_subscribed(user_id: int, use_cache=True) -> bool:
    now = asyncio.get_running_loop().time()
    if use_cache and subscription_cache.get(user_id, 0) > now:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        ok = member.status in {
            ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR
        }
        if ok:
            subscription_cache[user_id] = now + SUB_CACHE_SECONDS
        else:
            subscription_cache.pop(user_id, None)
        return ok
    except Exception:
        log.exception("Obunani tekshirish xatosi")
        subscription_cache.pop(user_id, None)
        return False

class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any]
    ) -> Any:
        if not event.from_user:
            return await handler(event, data)
        register_user(event.from_user.id)
        if event.text and event.text.split()[0].split("@")[0] == "/myid":
            return await handler(event, data)
        if await is_subscribed(event.from_user.id):
            return await handler(event, data)
        await event.answer(
            "⛔ ASAVED botidan foydalanish uchun avval Kino Olami kanaliga obuna bo‘ling.",
            reply_markup=subscribe_keyboard
        )
        return None

router.message.middleware(SubscriptionMiddleware())

@router.callback_query(F.data == "check_subscription")
async def check_sub_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    register_user(uid)
    if await is_subscribed(uid, use_cache=False):
        await callback.answer("✅ Obuna tasdiqlandi!")
        if callback.message:
            await callback.message.answer(main_text(), reply_markup=menu_for(uid))
    else:
        await callback.answer("❌ Avval kanalga obuna bo‘ling!", show_alert=True)

# ------------------------- BASIC COMMANDS -------------------------

@router.message(Command("myid"))
async def myid(message: Message):
    await message.answer(
        f"🆔 Telegram ID: <code>{message.from_user.id}</code>\n"
        "Render → Environment → ADMIN_IDS ga kiriting."
    )

@router.message(CommandStart())
async def start(message: Message):
    user_modes.pop(message.from_user.id, None)
    await message.answer(main_text(), reply_markup=menu_for(message.from_user.id))

@router.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Sizda admin huquqi yo‘q.")
        return
    await message.answer(
        f"🛠 <b>Admin panel</b>\n👥 Foydalanuvchilar: {len(all_user_ids())}\n"
        "Kino qo‘shish: ➕ Kino qo‘shish",
        reply_markup=admin_keyboard()
    )

# ------------------------- MOVIE MANAGEMENT -------------------------

@router.message(Command("addmovie"))
@router.message(F.text == BTN_ADD_MOVIE)
async def add_movie_start(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("⛔ Bu funksiya faqat admin uchun.")
        return
    movie_uploads[uid] = {"step": "number"}
    await message.answer("➕ Kino raqamini yuboring. Bekor qilish: /cancel")

@router.message(Command("cancel"))
async def cancel_upload(message: Message):
    movie_uploads.pop(message.from_user.id, None)
    await message.answer("❌ Amal bekor qilindi.", reply_markup=menu_for(message.from_user.id))

@router.message(Command("delmovie"))
async def del_movie(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo‘q.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Ishlatish: <code>/delmovie 6</code>")
        return
    await message.answer(
        "🗑 Kino o‘chirildi." if delete_movie(int(parts[1])) else "Bunday kino topilmadi."
    )

@router.message(F.text == BTN_MOVIE_SEARCH)
async def movie_search_start(message: Message):
    user_modes[message.from_user.id] = "movie_search"
    await message.answer("🎬 Kino nomini yoki raqamini yuboring.")

@router.callback_query(F.data.startswith("movie:"))
async def movie_callback(callback: CallbackQuery):
    if not await is_subscribed(callback.from_user.id, use_cache=False):
        await callback.answer("Avval kanalga obuna bo‘ling.", show_alert=True)
        return
    try:
        movie = get_movie(int(callback.data.split(":", 1)[1]))
    except (ValueError, IndexError):
        movie = None
    if not movie:
        await callback.answer("Kino topilmadi.", show_alert=True)
        return
    await callback.answer("🎬 Yuborilmoqda...")
    try:
        await send_movie(callback.from_user.id, movie)
    except Exception:
        log.exception("Kino yuborish xatosi")
        if callback.message:
            await callback.message.answer("❌ Kinoni yuborib bo‘lmadi.")

@router.message(F.photo)
async def movie_poster(message: Message):
    uid = message.from_user.id
    upload = movie_uploads.get(uid)
    if not upload or upload.get("step") != "poster":
        return
    upload["poster_file_id"] = message.photo[-1].file_id
    upload["step"] = "video"
    await message.answer("✅ Poster olindi. Endi kino videosini yoki video faylni yuboring.")

# ------------------------- VIDEO HELPERS -------------------------

def get_video_info(message: Message):
    if message.video:
        return message.video.file_id, message.video.file_size, message.video.duration, "video"
    if message.document and (message.document.mime_type or "").startswith("video/"):
        return message.document.file_id, message.document.file_size, None, "document"
    return None

async def run_ffmpeg(args: list[str]):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", "ignore")[-1000:] or "FFmpeg xatosi")

async def download_telegram_video(message: Message, target: Path):
    info = get_video_info(message)
    if not info:
        raise ValueError("Video topilmadi.")
    file_id, size, duration, _ = info
    if size and size > MAX_UPLOAD_BYTES:
        raise ValueError("Video 20 MB dan katta.")
    if duration and duration > MAX_SECONDS:
        raise ValueError("Video 10 daqiqadan uzun.")
    tg_file = await bot.get_file(file_id)
    await bot.download_file(tg_file.file_path, destination=target)
    return target

async def make_round(src: Path, dst: Path):
    await run_ffmpeg([
        "-i", str(src), "-t", "60",
        "-vf", "scale=640:640:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=640:640,setsar=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "0", "-crf", "27",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-movflags", "+faststart", str(dst)
    ])

async def make_916(src: Path, dst: Path):
    await run_ffmpeg([
        "-i", str(src),
        "-vf", "scale=720:1280:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=720:1280,setsar=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "0", "-crf", "26",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)
    ])

async def process_uploaded_video(message: Message, mode: str):
    status = await message.answer("⚡ Video tayyorlanmoqda...")
    uid = message.from_user.id
    try:
        async with VIDEO_SEMAPHORE:
            with tempfile.TemporaryDirectory(prefix="asaved_") as tmp:
                folder = Path(tmp)
                src, dst = folder / "input_video", folder / "output.mp4"
                await download_telegram_video(message, src)
                if mode == "round":
                    await make_round(src, dst)
                    if dst.stat().st_size > MAX_OUTPUT_BYTES:
                        raise ValueError("Tayyor video 49 MB dan katta.")
                    sent = await message.answer_video_note(FSInputFile(dst))
                    if sent.video_note:
                        save_user_video(uid, sent.video_note.file_id, "video_note", "Dumaloq video")
                else:
                    await make_916(src, dst)
                    if dst.stat().st_size > MAX_OUTPUT_BYTES:
                        raise ValueError("Tayyor video 49 MB dan katta.")
                    sent = await message.answer_video(FSInputFile(dst), caption="📱 9:16 format tayyor!")
                    if sent.video:
                        save_user_video(uid, sent.video.file_id, "video", "9:16 video")
        await status.delete()
        await message.answer("💾 Video «Mening videolarim» bo‘limiga saqlandi.")
    except Exception as exc:
        log.exception("Video qayta ishlash xatosi")
        await status.edit_text(f"❌ Xatolik: {html.escape(str(exc)[:500])}")
    finally:
        user_modes.pop(uid, None)

# ------------------------- SAVED VIDEOS -------------------------

@router.message(F.text == BTN_MY_VIDEOS)
async def my_videos(message: Message):
    rows = get_user_videos(message.from_user.id)
    if not rows:
        await message.answer("📁 Hozircha saqlangan videolaringiz yo‘q.")
        return
    buttons = []
    for video_id, label, media_type, created_at in rows:
        buttons.append([InlineKeyboardButton(
            text=f"📹 {label[:40]}",
            callback_data=f"saved:{video_id}"
        )])
    await message.answer("📁 <b>Oxirgi 15 ta saqlangan videongiz:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("saved:"))
async def saved_video_callback(callback: CallbackQuery):
    try:
        video_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Noto‘g‘ri so‘rov.")
        return
    row = get_saved_video(video_id, callback.from_user.id)
    if not row:
        await callback.answer("Video topilmadi.", show_alert=True)
        return
    file_id, media_type, label = row
    await callback.answer("📹 Video yuborilmoqda...")
    try:
        await send_saved_file(callback.from_user.id, file_id, media_type, f"📁 {html.escape(label)}")
    except Exception:
        log.exception("Saqlangan video yuborilmadi")
        if callback.message:
            await callback.message.answer("❌ Videoni qayta yuborib bo‘lmadi.")

# ------------------------- VIDEO MODE BUTTONS -------------------------

@router.message(F.text == BTN_ROUND)
async def choose_round(message: Message):
    user_modes[message.from_user.id] = "round"
    await message.answer("🎥 Galereyadan 60 soniyagacha video yuboring (20 MB gacha).")

@router.message(F.text == BTN_916)
async def choose_916(message: Message):
    user_modes[message.from_user.id] = "916"
    await message.answer("📱 9:16 formatga o‘tkazish uchun video yuboring (20 MB gacha).")

@router.message(F.text == BTN_LINK)
async def choose_link(message: Message):
    user_modes[message.from_user.id] = "link"
    await message.answer("🔗 YouTube, Instagram yoki TikTok havolasini yuboring.")

@router.message(F.text == BTN_TTS)
async def choose_tts(message: Message):
    user_modes[message.from_user.id] = "voice_choose"
    await message.answer("🎙️ Ovoz turini tanlang:", reply_markup=voice_keyboard)

@router.message(F.text == BTN_MALE)
async def choose_male(message: Message):
    user_modes[message.from_user.id] = "tts_male"
    await message.answer("👨 Erkak ovozi tanlandi. Matn yuboring.", reply_markup=menu_for(message.from_user.id))

@router.message(F.text == BTN_FEMALE)
async def choose_female(message: Message):
    user_modes[message.from_user.id] = "tts_female"
    await message.answer("👩 Ayol ovozi tanlandi. Matn yuboring.", reply_markup=menu_for(message.from_user.id))

@router.message(F.text == BTN_MENU)
async def back_menu(message: Message):
    user_modes.pop(message.from_user.id, None)
    await message.answer(main_text(), reply_markup=menu_for(message.from_user.id))

# ------------------------- LINK DOWNLOADER -------------------------

def valid_video_url(raw: str) -> bool:
    try:
        parsed = urlparse(raw.strip())
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and (
            host in ALLOWED_HOSTS or any(host.endswith("." + h) for h in ALLOWED_HOSTS)
        )
    except Exception:
        return False

def detect_platform(url: str):
    host = (urlparse(url).hostname or "").lower()
    if "instagram.com" in host:
        return "Instagram"
    if "tiktok.com" in host:
        return "TikTok"
    return "YouTube"

async def download_public_link(url: str, folder: Path) -> Path:
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp o‘rnatilmagan.")
    opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": str(folder / "link_video.%(ext)s"),
        "noplaylist": True,
        "max_filesize": MAX_LINK_BYTES,
        "socket_timeout": 20,
        "retries": 1,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "remuxvideo": "mp4",
    }
    def work():
        import yt_dlp
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    await asyncio.to_thread(work)
    files = [p for p in folder.iterdir() if p.is_file() and p.name.startswith("link_video.")]
    if not files:
        raise RuntimeError("Video yuklab olinmadi.")
    result = max(files, key=lambda p: p.stat().st_size)
    if result.stat().st_size > MAX_LINK_BYTES:
        raise RuntimeError("Video 49 MB dan katta.")
    return result

# ------------------------- ELEVENLABS TTS -------------------------

async def text_to_speech(text: str, voice_id: str, destination: Path):
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY kiritilmagan.")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Accept": "audio/mpeg", "Content-Type": "application/json"}
    payload = {
        "text": text, "model_id": TTS_MODEL_ID,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
    }
    timeout = ClientTimeout(total=120, connect=20)
    async with ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                detail = (await response.text())[:300]
                log.error("ElevenLabs %s: %s", response.status, detail)
                if response.status == 401:
                    raise RuntimeError("ElevenLabs API kaliti noto‘g‘ri.")
                if response.status == 429:
                    raise RuntimeError("ElevenLabs limiti tugagan.")
                raise RuntimeError(f"ElevenLabs API xatosi: {response.status}")
            data = await response.read()
    if not data:
        raise RuntimeError("Audio bo‘sh qaytdi.")
    destination.write_bytes(data)

async def process_tts(message: Message, text: str, mode: str):
    if not text.strip():
        await message.answer("Matn yuboring.")
        return
    if len(text) > MAX_TTS_CHARS:
        await message.answer(f"Matn {MAX_TTS_CHARS} belgidan oshmasin.")
        return
    voice_key = "male" if mode == "tts_male" else "female"
    status = await message.answer("🎙️ Matn ovozga aylantirilmoqda...")
    try:
        async with TTS_SEMAPHORE:
            with tempfile.TemporaryDirectory(prefix="asaved_tts_") as tmp:
                audio = Path(tmp) / "voice.mp3"
                await text_to_speech(text, TTS_VOICES[voice_key], audio)
                sent = await message.answer_audio(
                    FSInputFile(audio), title="ASAVED AI ovoz",
                    caption=f"✅ {'Erkak' if voice_key == 'male' else 'Ayol'} AI ovozi"
                )
                if sent.audio:
                    save_user_video(message.from_user.id, sent.audio.file_id, "audio", "AI ovoz")
        await status.delete()
        await message.answer("💾 Audio ham «Mening videolarim» bo‘limiga saqlandi.")
    except Exception as exc:
        log.exception("TTS xatosi")
        await status.edit_text(f"❌ Ovoz yaratilmadi: {html.escape(str(exc)[:300])}")
    finally:
        user_modes.pop(message.from_user.id, None)

# ------------------------- BROADCAST -------------------------

@router.message(F.text == BTN_BROADCAST)
async def broadcast_start(message: Message):
    if not is_admin(message.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ha, yuborish", callback_data="broadcast:yes")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="broadcast:no")]
    ])
    await message.answer(f"📢 Xabar {len(all_user_ids())} foydalanuvchiga yuboriladi. Davom etasizmi?", reply_markup=kb)

@router.callback_query(F.data == "broadcast:no")
async def broadcast_cancel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo‘q.", show_alert=True)
        return
    await callback.answer("Bekor qilindi.")
    if callback.message:
        await callback.message.edit_text("❌ Reklama bekor qilindi.")

@router.callback_query(F.data == "broadcast:yes")
async def broadcast_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo‘q.", show_alert=True)
        return
    await callback.answer("Yuborish boshlandi.")
    if callback.message:
        await callback.message.edit_text("⏳ Yuborilmoqda...")
    ids = all_user_ids()
    sent = failed = 0
    for uid in ids:
        try:
            await bot.send_message(uid, AD_TEXT)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    if callback.message:
        await callback.message.answer(
            f"✅ Yakunlandi.\n👥 Jami: {len(ids)}\n✅ Yuborildi: {sent}\n❌ Xato: {failed}",
            reply_markup=admin_keyboard()
        )

# ------------------------- CENTRAL TEXT HANDLER -------------------------

@router.message(F.text)
async def handle_text(message: Message):
    text = (message.text or "").strip()
    uid = message.from_user.id
    mode = user_modes.get(uid)

    # Kino qo‘shish wizard
    upload = movie_uploads.get(uid)
    if upload:
        step = upload.get("step")
        if step == "number":
            if not text.isdigit() or int(text) < 1:
                await message.answer("Musbat raqam yuboring. Masalan: 6")
                return
            upload["number"] = int(text)
            upload["step"] = "title"
            await message.answer("🎬 Kino nomini yuboring.")
            return
        if step == "title":
            if not text or len(text) > 150:
                await message.answer("Kino nomi 1–150 belgi bo‘lsin.")
                return
            upload["title"] = text
            upload["step"] = "description"
            await message.answer("📝 Tavsif yuboring yoki /skip yozing.")
            return
        if step == "description":
            upload["description"] = "" if text == "/skip" else text[:800]
            upload["step"] = "poster"
            await message.answer("🖼 Poster rasmini yuboring yoki /skip yozing.")
            return
        if step == "poster":
            if text == "/skip":
                upload["poster_file_id"] = None
                upload["step"] = "video"
                await message.answer("🎞 Endi film videosini yuboring.")
            else:
                await message.answer("Poster rasmini yuboring yoki /skip yozing.")
            return
        if step == "video":
            await message.answer("🎞 Kino videosini video yoki video fayl ko‘rinishida yuboring.")
            return

    if mode == "movie_search":
        user_modes.pop(uid, None)
        results = search_movies(text)
        if not results:
            await message.answer("😕 Bunday kino topilmadi.")
            return
        buttons = [[InlineKeyboardButton(text=f"🎬 {n}. {title[:45]}", callback_data=f"movie:{mid}")]
                   for mid, n, title, desc in results]
        await message.answer("🔎 Topilgan kinolar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        return

    if mode in ("tts_male", "tts_female"):
        await process_tts(message, text, mode)
        return
    if mode == "voice_choose":
        await message.answer("Avval erkak yoki ayol ovozini tanlang.", reply_markup=voice_keyboard)
        return

    if mode == "link":
        if not valid_video_url(text):
            await message.answer("❌ HTTPS YouTube, Instagram yoki TikTok havolasini yuboring.")
            return
        status = await message.answer("⚡ Havoladagi video yuklanmoqda...")
        try:
            with tempfile.TemporaryDirectory(prefix="asaved_link_") as tmp:
                async with VIDEO_SEMAPHORE:
                    path = await download_public_link(text, Path(tmp))
                    sent = await message.answer_video(FSInputFile(path), caption=f"✅ {detect_platform(text)} videosi tayyor!")
                    if sent.video:
                        save_user_video(uid, sent.video.file_id, "video", "Havoladan video")
            await status.delete()
            await message.answer("💾 Video «Mening videolarim» bo‘limiga saqlandi.")
        except Exception as exc:
            log.exception("Link video xatosi")
            await status.edit_text(f"❌ Yuklab bo‘lmadi: {html.escape(str(exc)[:300])}")
        finally:
            user_modes.pop(uid, None)
        return

    if mode in ("round", "916"):
        await message.answer("Iltimos, galereyadan video yuboring.")
        return

    # Oddiy raqam yuborilsa ham kino izlash
    if text.isdigit():
        results = search_movies(text)
        if results:
            buttons = [[InlineKeyboardButton(text=f"🎬 {n}. {title[:45]}", callback_data=f"movie:{mid}")]
                       for mid, n, title, desc in results]
            await message.answer("🎬 Kino topildi:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            return

    await message.answer("Menyudan kerakli funksiyani tanlang.", reply_markup=menu_for(uid))

# ------------------------- MEDIA HANDLER -------------------------

@router.message(F.video | F.document)
async def handle_video_or_document(message: Message):
    uid = message.from_user.id
    upload = movie_uploads.get(uid)

    # Admin kino qo‘shish bosqichi
    if upload and upload.get("step") == "video":
        info = get_video_info(message)
        if not info:
            await message.answer("❌ Video fayl yuboring. Fayl turi video/mp4 bo‘lishi kerak.")
            return
        file_id, size, duration, media_type = info
        try:
            save_movie(
                upload["number"], upload["title"], upload.get("description", ""),
                file_id, media_type, upload.get("poster_file_id")
            )
            number, title = upload["number"], upload["title"]
            movie_uploads.pop(uid, None)
            await message.answer(
                f"✅ <b>Kino saqlandi!</b>\n🔢 Raqami: {number}\n🎬 Nomi: {html.escape(title)}",
                reply_markup=admin_keyboard()
            )
        except Exception:
            log.exception("Movie DB save failed")
            await message.answer("❌ Kino bazaga saqlanmadi.")
        return

    mode = user_modes.get(uid)
    if mode not in ("round", "916"):
        await message.answer("Avval 🎥 Dumaloq video yoki 📱 9:16 format tugmasini tanlang.")
        return
    if not get_video_info(message):
        await message.answer("Video fayl yuboring.")
        return
    await process_uploaded_video(message, mode)

# ------------------------- FALLBACK -------------------------

@router.message()
async def fallback(message: Message):
    await message.answer("Menyudan kerakli funksiyani tanlang.", reply_markup=menu_for(message.from_user.id))

# ------------------------- WEBHOOK -------------------------

async def on_startup(app: web.Application):
    init_db()
    await bot.set_webhook(
        f"{BASE_URL}/{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types()
    )
    log.info("ASAVED webhook o‘rnatildi.")

async def on_shutdown(app: web.Application):
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.session.close()

async def main():
    init_db()
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_get("/", lambda request: web.Response(text="ASAVED bot is running"))
    app.router.add_get("/health", lambda request: web.Response(text="ok"))
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(
        app, path=f"/{WEBHOOK_PATH}"
    )
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_shutdown)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("ASAVED port %s da ishga tushdi", PORT)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
