
import asyncio
import logging
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, Update, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, FSInputFile
)
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import yt_dlp


# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = 8461889678
CHANNEL = "@akoshsaved"

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET", "asaved_webhook_secret_2026"
).strip()
WEBHOOK_PATH = os.getenv(
    "WEBHOOK_PATH", "asaved-webhook"
).strip("/")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

DB_PATH = os.getenv("DB_PATH", "/data/asaved.sqlite3")

MAX_VIDEO_SIZE = 20 * 1024 * 1024
MAX_DOWNLOAD_SIZE = 48 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("ASAVED")

if not BOT_TOKEN:
    raise RuntimeError("Render Environment Variables ichida BOT_TOKEN yo'q")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

user_modes = {}


# ================= DATABASE =================

def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                lang TEXT DEFAULT 'uz',
                pro_until TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                days INTEGER,
                amount INTEGER,
                file_id TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)
        conn.commit()


def user_row(uid):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id=?",
            (uid,)
        ).fetchone()
        return dict(row) if row else None


def ensure_user(uid):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(user_id,lang) VALUES(?,?)",
            (uid, "uz")
        )
        conn.commit()


def set_lang(uid, lang):
    ensure_user(uid)
    with db() as conn:
        conn.execute(
            "UPDATE users SET lang=? WHERE user_id=?",
            (lang, uid)
        )
        conn.commit()


def is_pro(uid):
    row = user_row(uid)
    if not row or not row.get("pro_until"):
        return False
    try:
        expiry = datetime.fromisoformat(row["pro_until"])
        return expiry > datetime.now(timezone.utc)
    except Exception:
        return False


def pro_until_text(uid):
    row = user_row(uid)
    if not row or not row.get("pro_until"):
        return "Faol emas"
    try:
        expiry = datetime.fromisoformat(row["pro_until"])
        if expiry <= datetime.now(timezone.utc):
            return "Muddati tugagan"
        return expiry.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "Faol emas"


def add_payment(uid, days, amount, file_id):
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO payments
            (user_id,days,amount,file_id,status,created_at)
            VALUES(?,?,?,?,?,?)
        """, (
            uid, days, amount, file_id, "pending",
            datetime.now(timezone.utc).isoformat()
        ))
        conn.commit()
        return cur.lastrowid


def approve_payment(pid):
    with db() as conn:
        payment = conn.execute(
            "SELECT * FROM payments WHERE id=? AND status='pending'",
            (pid,)
        ).fetchone()

        if not payment:
            return None

        uid = payment["user_id"]
        days = payment["days"]

        row = conn.execute(
            "SELECT pro_until FROM users WHERE user_id=?",
            (uid,)
        ).fetchone()

        now = datetime.now(timezone.utc)
        start = now

        if row and row["pro_until"]:
            try:
                old = datetime.fromisoformat(row["pro_until"])
                if old > now:
                    start = old
            except Exception:
                pass

        expiry = start + timedelta(days=days)

        conn.execute(
            "UPDATE users SET pro_until=? WHERE user_id=?",
            (expiry.isoformat(), uid)
        )
        conn.execute(
            "UPDATE payments SET status='approved' WHERE id=?",
            (pid,)
        )
        conn.commit()

        return uid, expiry


def reject_payment(pid):
    with db() as conn:
        payment = conn.execute(
            "SELECT user_id FROM payments WHERE id=? AND status='pending'",
            (pid,)
        ).fetchone()

        if not payment:
            return None

        conn.execute(
            "UPDATE payments SET status='rejected' WHERE id=?",
            (pid,)
        )
        conn.commit()
        return payment["user_id"]


# ================= LANGUAGES =================

TEXT = {
    "uz": {
        "choose": "Tilni tanlang:",
        "menu": "ASAVED botiga xush kelibsiz! Kerakli xizmatni tanlang.",
        "round": "🎥 Dumaloq video",
        "link": "🔗 Video havolasi",
        "format": "📱 9:16 format",
        "pro": "⭐ PRO sotib olish",
        "status": "👤 PRO holati",
        "help": "ℹ️ Yordam",
        "roundask": "Dumaloq qilmoqchi bo‘lgan videoni yuboring.",
        "formatask": "9:16 formatga o‘tkazish uchun videoni yuboring.",
        "linkask": "Video havolasini yuboring.",
        "receipt": "Chekingiz adminga yuborildi. Tasdiqlanishini kuting.",
        "wait": "To‘lov cheki sifatida rasm yoki PDF yuboring.",
        "approved": "✅ To‘lov tasdiqlandi! PRO faollashtirildi.",
        "rejected": "❌ To‘lov chekingiz rad etildi.",
        "notpro": "Bu imkoniyat faqat PRO foydalanuvchilar uchun.",
        "rights": "Faqat o‘zingizga tegishli yoki yuklab olishga ruxsatingiz bor videolarni yuboring.",
        "badurl": "Havola noto‘g‘ri. YouTube, Instagram yoki TikTok havolasini yuboring.",
        "fail": "❌ Video bilan ishlashda xatolik yuz berdi.",
        "too_big": "❌ Video hajmi juda katta.",
        "round_done": "✅ Dumaloq video tayyor!",
        "916_done": "✅ 9:16 video tayyor!",
        "unknown": "Kerakli tugmani tanlang.",
        "join": "Botdan foydalanish uchun kanalga obuna bo‘ling.",
        "check": "✅ Obunani tekshirish",
        "subok": "Obuna tasdiqlandi!",
        "subno": "Avval kanalga obuna bo‘ling.",
        "audio": "🎵 Instagram MP3",
        "audioask": "Instagram video havolasini yuboring. Faqat PRO uchun.",
        "proactive": "PRO faol",
        "proexpired": "PRO faol emas",
    },
    "ru": {
        "choose": "Выберите язык:",
        "menu": "Добро пожаловать в ASAVED! Выберите функцию.",
        "round": "🎥 Круглое видео",
        "link": "🔗 Ссылка на видео",
        "format": "📱 Формат 9:16",
        "pro": "⭐ Купить PRO",
        "status": "👤 Статус PRO",
        "help": "ℹ️ Помощь",
        "roundask": "Отправьте видео для круглого формата.",
        "formatask": "Отправьте видео для формата 9:16.",
        "linkask": "Отправьте ссылку на видео.",
        "receipt": "Чек отправлен администратору.",
        "wait": "Отправьте чек фото или PDF.",
        "approved": "✅ Оплата подтверждена! PRO активирован.",
        "rejected": "❌ Платёж отклонён.",
        "notpro": "Эта функция доступна только PRO.",
        "rights": "Отправляйте только видео, на скачивание которых у вас есть разрешение.",
        "badurl": "Отправьте ссылку YouTube, Instagram или TikTok.",
        "fail": "❌ Ошибка обработки видео.",
        "too_big": "❌ Видео слишком большое.",
        "round_done": "✅ Круглое видео готово!",
        "916_done": "✅ Видео 9:16 готово!",
        "unknown": "Выберите кнопку.",
        "join": "Подпишитесь на канал для использования бота.",
        "check": "✅ Проверить подписку",
        "subok": "Подписка подтверждена!",
        "subno": "Сначала подпишитесь на канал.",
        "audio": "🎵 Instagram MP3",
        "audioask": "Отправьте ссылку Instagram. Только PRO.",
        "proactive": "PRO активен",
        "proexpired": "PRO не активен",
    },
    "en": {
        "choose": "Choose your language:",
        "menu": "Welcome to ASAVED! Choose a service.",
        "round": "🎥 Round video",
        "link": "🔗 Video link",
        "format": "📱 9:16 format",
        "pro": "⭐ Buy PRO",
        "status": "👤 PRO status",
        "help": "ℹ️ Help",
        "roundask": "Send a video to make it round.",
        "formatask": "Send a video for 9:16 format.",
        "linkask": "Send a video link.",
        "receipt": "Your receipt was sent to the admin.",
        "wait": "Send your receipt as a photo or PDF.",
        "approved": "✅ Payment approved! PRO activated.",
        "rejected": "❌ Payment rejected.",
        "notpro": "This feature is for PRO users only.",
        "rights": "Only send videos you own or have permission to download.",
        "badurl": "Send a YouTube, Instagram or TikTok link.",
        "fail": "❌ Video processing failed.",
        "too_big": "❌ Video is too large.",
        "round_done": "✅ Round video is ready!",
        "916_done": "✅ 9:16 video is ready!",
        "unknown": "Choose a button.",
        "join": "Subscribe to the channel to use the bot.",
        "check": "✅ Check subscription",
        "subok": "Subscription confirmed!",
        "subno": "Please subscribe first.",
        "audio": "🎵 Instagram MP3",
        "audioask": "Send an Instagram video link. PRO only.",
        "proactive": "PRO active",
        "proexpired": "PRO inactive",
    }
}


def tr(lang, key):
    return TEXT.get(lang, TEXT["uz"]).get(
        key, TEXT["uz"].get(key, key)
    )


def lang_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇺🇿 O‘zbekcha", callback_data="lang:uz"),
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru")
        ],
        [
            InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en")
        ]
    ])


def main_keyboard(lang):
    t = TEXT.get(lang, TEXT["uz"])
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t["round"]), KeyboardButton(text=t["format"])],
            [KeyboardButton(text=t["link"]), KeyboardButton(text=t["audio"])],
            [KeyboardButton(text=t["pro"]), KeyboardButton(text=t["status"])],
            [KeyboardButton(text=t["help"])]
        ],
        resize_keyboard=True
    )


def join_keyboard(lang):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📢 @akoshsaved",
            url="https://t.me/akoshsaved"
        )],
        [InlineKeyboardButton(
            text=tr(lang, "check"),
            callback_data="checksub"
        )]
    ])


# ================= ACCESS =================

async def subscribed(uid):
    try:
        member = await bot.get_chat_member(CHANNEL, uid)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        log.exception("Channel membership check failed")
        return False


async def require_access(message, lang):
    if is_pro(message.from_user.id):
        return True

    if await subscribed(message.from_user.id):
        return True

    await message.answer(
        tr(lang, "join"),
        reply_markup=join_keyboard(lang)
    )
    return False


async def show_menu(message, lang):
    await message.answer(
        tr(lang, "menu"),
        reply_markup=main_keyboard(lang)
    )


# ================= START / LANGUAGE =================

@router.message(CommandStart())
async def start_handler(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    row = user_row(uid)

    if not row or not row["lang"]:
        await message.answer(
            "🌍 Tilni tanlang / Выберите язык / Choose language:",
            reply_markup=lang_keyboard()
        )
        return

    lang = row["lang"]
    if not await require_access(message, lang):
        return

    await show_menu(message, lang)


@router.callback_query(F.data.startswith("lang:"))
async def language_callback(call: CallbackQuery):
    lang = call.data.split(":")[1]
    if lang not in ("uz", "ru", "en"):
        await call.answer()
        return

    set_lang(call.from_user.id, lang)
    await call.answer("OK")
    await call.message.answer(
        tr(lang, "menu"),
        reply_markup=main_keyboard(lang)
    )


@router.callback_query(F.data == "checksub")
async def check_sub_callback(call: CallbackQuery):
    uid = call.from_user.id
    row = user_row(uid)
    lang = row["lang"] if row else "uz"

    if is_pro(uid) or await subscribed(uid):
        await call.answer(tr(lang, "subok"), show_alert=True)
        await call.message.answer(
            tr(lang, "menu"),
            reply_markup=main_keyboard(lang)
        )
    else:
        await call.answer(tr(lang, "subno"), show_alert=True)


# ================= PRO PAYMENT =================

def plans_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="7 kun — 30 000 so‘m",
            callback_data="buy:7:30000"
        )],
        [InlineKeyboardButton(
            text="30 kun — 55 000 so‘m",
            callback_data="buy:30:55000"
        )]
    ])


@router.callback_query(F.data.startswith("buy:"))
async def buy_plan(call: CallbackQuery):
    uid = call.from_user.id
    row = user_row(uid)
    lang = row["lang"] if row else "uz"

    try:
        _, days, amount = call.data.split(":")
        days = int(days)
        amount = int(amount)
    except Exception:
        await call.answer("Xatolik", show_alert=True)
        return

    card = os.getenv("PAYMENT_CARD", "").strip()
    holder = os.getenv("PAYMENT_HOLDER", "").strip()

    if not card or not holder:
        await call.answer("To‘lov sozlamalari tayyor emas.", show_alert=True)
        return

    user_modes[uid] = f"receipt:{days}:{amount}"

    await call.message.answer(
        f"⭐ <b>ASAVED PRO</b>\n\n"
        f"Tarif: {days} kun\n"
        f"Narxi: {amount:,} so‘m\n\n"
        f"💳 Karta: <code>{card}</code>\n"
        f"👤 Karta egasi: {holder}\n\n"
        f"To‘lovdan so‘ng chekni rasm yoki PDF shaklida yuboring."
        .replace(",", " ")
    )
    await call.answer()


# ================= ADMIN =================

@router.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    with db() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM payments WHERE status='pending'"
        ).fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM users WHERE pro_until > ?",
            (datetime.now(timezone.utc).isoformat(),)
        ).fetchone()[0]
        users = conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

    await message.answer(
        f"🛠 <b>ADMIN PANEL</b>\n\n"
        f"👥 Foydalanuvchilar: {users}\n"
        f"⭐ Faol PRO: {active}\n"
        f"⏳ Kutilayotgan to‘lovlar: {pending}"
    )


@router.callback_query(F.data.startswith("payok:"))
async def approve_callback(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Ruxsat yo‘q", show_alert=True)
        return

    try:
        pid = int(call.data.split(":")[1])
    except Exception:
        await call.answer("Xato ID", show_alert=True)
        return

    result = approve_payment(pid)
    if not result:
        await call.answer("Bu chek avval ko‘rib chiqilgan.", show_alert=True)
        return

    uid, expiry = result
    row = user_row(uid)
    lang = row["lang"] if row else "uz"

    try:
        await bot.send_message(
            uid,
            tr(lang, "approved") +
            f"\nPRO muddati: {expiry.strftime('%d.%m.%Y %H:%M')}"
        )
    except Exception:
        log.exception("User approval notification failed")

    try:
        await call.message.edit_caption(
            caption=(call.message.caption or "") + "\n\n✅ TASDIQLANDI",
            reply_markup=None
        )
    except Exception:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    await call.answer("PRO faollashtirildi!")


@router.callback_query(F.data.startswith("payno:"))
async def reject_callback(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Ruxsat yo‘q", show_alert=True)
        return

    try:
        pid = int(call.data.split(":")[1])
    except Exception:
        await call.answer("Xato ID", show_alert=True)
        return

    uid = reject_payment(pid)
    if not uid:
        await call.answer("Bu chek avval ko‘rib chiqilgan.", show_alert=True)
        return

    row = user_row(uid)
    lang = row["lang"] if row else "uz"

    try:
        await bot.send_message(uid, tr(lang, "rejected"))
    except Exception:
        log.exception("User rejection notification failed")

    try:
        await call.message.edit_caption(
            caption=(call.message.caption or "") + "\n\n❌ RAD ETILDI",
            reply_markup=None
        )
    except Exception:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    await call.answer("To‘lov rad etildi.")


# ================= VIDEO CONVERSION =================

async def run_ffmpeg(input_path, output_path, mode):
    if mode == "round":
        filters = (
            "scale=480:480:force_original_aspect_ratio=increase,"
            "crop=480:480"
        )
        extra = ["-t", "60"]
    else:
        filters = (
            "scale=540:960:force_original_aspect_ratio=increase,"
            "crop=540:960"
        )
        extra = []

    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-vf", filters,
        *extra,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "25",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path)
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()

    if process.returncode != 0:
        log.error("FFmpeg error: %s", stderr.decode(errors="ignore")[-2000:])
        raise RuntimeError("FFmpeg conversion failed")


async def process_uploaded(message: Message, mode: str, lang: str):
    video = message.video
    if not video:
        await message.answer("Iltimos, videoni oddiy video sifatida yuboring.")
        return

    if video.file_size and video.file_size > MAX_VIDEO_SIZE:
        await message.answer(tr(lang, "too_big"))
        return

    status = await message.answer("⏳ Video tayyorlanmoqda...")

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "input.mp4"
        output = Path(tmp) / "output.mp4"

        try:
            file = await bot.get_file(video.file_id)
            await bot.download_file(file.file_path, destination=source)

            await run_ffmpeg(source, output, mode)

            if mode == "round":
                await message.answer_video_note(
                    video_note=FSInputFile(output)
                )
                await status.edit_text(tr(lang, "round_done"))
            else:
                await message.answer_video(
                    video=FSInputFile(output),
                    caption=tr(lang, "916_done")
                )
                await status.edit_text("✅ Tayyor!")

        except Exception:
            log.exception("Uploaded video processing failed")
            await status.edit_text(tr(lang, "fail"))


@router.message(F.video)
async def video_handler(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    row = user_row(uid)
    lang = row["lang"] if row else "uz"
    mode = user_modes.get(uid)

    if mode not in ("round", "916"):
        await message.answer(tr(lang, "unknown"))
        return

    if not await require_access(message, lang):
        return

    user_modes.pop(uid, None)
    await process_uploaded(message, mode, lang)


@router.message(F.video_note)
async def video_note_handler(message: Message):
    row = user_row(message.from_user.id)
    lang = row["lang"] if row else "uz"
    await message.answer(
        "Bu video allaqachon dumaloq formatda."
    )


# ================= VIDEO LINK DOWNLOAD =================

def valid_video_url(url):
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        allowed = (
            host == "youtube.com"
            or host.endswith(".youtube.com")
            or host == "youtu.be"
            or host == "instagram.com"
            or host.endswith(".instagram.com")
            or host == "tiktok.com"
            or host.endswith(".tiktok.com")
        )
        return parsed.scheme == "https" and allowed
    except Exception:
        return False


def download_media_sync(url, folder, audio=False):
    folder = Path(folder)
    output_template = str(folder / "%(id)s.%(ext)s")

    options = {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_DOWNLOAD_SIZE,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 25,
        "overwrites": True,
    }

    if audio:
        options.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        })
    else:
        options.update({
            "format": "best[ext=mp4]/best",
            "merge_output_format": "mp4",
        })

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    extension = "*.mp3" if audio else "*.mp4"
    files = list(folder.glob(extension))

    if not files:
        files = [
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() not in (".part", ".ytdl")
        ]

    if not files:
        raise RuntimeError("Downloaded file not found")

    result = max(files, key=lambda p: p.stat().st_size)

    if result.stat().st_size > MAX_DOWNLOAD_SIZE:
        raise RuntimeError("Downloaded file is too large")

    return result


async def process_link_message(message: Message, lang: str, audio=False):
    url = (message.text or "").strip()

    if not valid_video_url(url):
        await message.answer(tr(lang, "badurl"))
        return

    host = (urlparse(url).hostname or "").lower()

    if audio and not (
        host == "instagram.com" or host.endswith(".instagram.com")
    ):
        await message.answer(
            "MP3 funksiyasi faqat Instagram havolalari uchun."
        )
        return

    if audio and not is_pro(message.from_user.id):
        await message.answer(tr(lang, "notpro"))
        return

    if not await require_access(message, lang):
        return

    await message.answer(tr(lang, "rights"))
    status = await message.answer("⏳ Havola tekshirilmoqda...")

    with tempfile.TemporaryDirectory() as tmp:
        try:
            path = await asyncio.to_thread(
                download_media_sync, url, tmp, audio
            )

            if audio:
                await message.answer_audio(
                    audio=FSInputFile(path),
                    caption="✅ MP3 tayyor!"
                )
            else:
                await message.answer_video(
                    video=FSInputFile(path),
                    caption="✅ Video tayyor!"
                )

            await status.edit_text("✅ Bajarildi!")

        except Exception as exc:
            log.exception("URL download failed: %s", exc)
            await status.edit_text(
                "❌ Yuklab bo‘lmadi. Havola ochiq ekanini tekshiring. "
                "Ba’zi platformalar yoki videolar qo‘llab-quvvatlanmasligi mumkin."
            )


# ================= RECEIPTS =================

@router.message(F.photo | F.document)
async def receipt_handler(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    row = user_row(uid)
    lang = row["lang"] if row else "uz"
    mode = user_modes.get(uid)

    if not isinstance(mode, str) or not mode.startswith("receipt:"):
        await message.answer(tr(lang, "unknown"))
        return

    try:
        _, days, amount = mode.split(":")
        days = int(days)
        amount = int(amount)
    except Exception:
        await message.answer("❌ To‘lov ma’lumotida xatolik.")
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        is_photo = True
    elif message.document:
        doc = message.document
        if doc.mime_type != "application/pdf":
            await message.answer(
                "Chekni rasm yoki PDF ko‘rinishida yuboring."
            )
            return
        file_id = doc.file_id
        is_photo = False
    else:
        return

    try:
        pid = add_payment(uid, days, amount, file_id)
    except Exception:
        log.exception("Payment database error")
        await message.answer("❌ Chekni saqlashda xatolik.")
        return

    plan = f"{days} kun / {amount:,} so‘m".replace(",", " ")
    caption = (
        "💳 <b>YANGI PRO TO‘LOV</b>\n\n"
        f"👤 User ID: <code>{uid}</code>\n"
        f"📦 Tarif: {plan}\n"
        f"🧾 Chek ID: {pid}\n\n"
        "To‘lovni tekshiring:"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Tasdiqlash",
                callback_data=f"payok:{pid}"
            ),
            InlineKeyboardButton(
                text="❌ Rad etish",
                callback_data=f"payno:{pid}"
            )
        ]
    ])

    try:
        if is_photo:
            await bot.send_photo(
                chat_id=ADMIN_ID,
                photo=file_id,
                caption=caption,
                reply_markup=keyboard
            )
        else:
            await bot.send_document(
                chat_id=ADMIN_ID,
                document=file_id,
                caption=caption,
                reply_markup=keyboard
            )

    except Exception:
        log.exception("ADMIN RECEIPT DELIVERY FAILED")
        reject_payment(pid)
        await message.answer(
            "❌ Chek adminga yuborilmadi. "
            "Admin botga /start bosganini tekshiring."
        )
        return

    user_modes.pop(uid, None)
    await message.answer(tr(lang, "receipt"))


# ================= TEXT ROUTER =================

@router.message(F.text)
async def text_router(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    row = user_row(uid)
    lang = row["lang"] if row else "uz"
    text = message.text.strip()
    mode = user_modes.get(uid)

    if mode == "receipt" or (
        isinstance(mode, str) and mode.startswith("receipt:")
    ):
        await message.answer(tr(lang, "wait"))
        return

    if mode in ("round", "916"):
        await message.answer(
            "Iltimos, videoni oddiy video sifatida yuboring."
        )
        return

    if mode == "link":
        await process_link_message(message, lang, audio=False)
        user_modes.pop(uid, None)
        return

    if mode == "audio":
        await process_link_message(message, lang, audio=True)
        user_modes.pop(uid, None)
        return

    t = TEXT.get(lang, TEXT["uz"])

    mapping = {
        t["round"]: "round",
        t["format"]: "916",
        t["link"]: "link",
        t["audio"]: "audio",
        t["pro"]: "pro",
        t["status"]: "status",
        t["help"]: "help",
    }

    action = mapping.get(text)

    if action == "round":
        if not await require_access(message, lang):
            return
        user_modes[uid] = "round"
        await message.answer(tr(lang, "roundask"))
        return

    if action == "916":
        if not await require_access(message, lang):
            return
        user_modes[uid] = "916"
        await message.answer(tr(lang, "formatask"))
        return

    if action == "link":
        if not await require_access(message, lang):
            return
        user_modes[uid] = "link"
        await message.answer(tr(lang, "linkask"))
        return

    if action == "audio":
        if not is_pro(uid):
            await message.answer(tr(lang, "notpro"))
            return
        user_modes[uid] = "audio"
        await message.answer(tr(lang, "audioask"))
        return

    if action == "pro":
        await message.answer(
            "⭐ <b>ASAVED PRO</b>\n\n"
            "7 kun — 30 000 so‘m\n"
            "30 kun — 55 000 so‘m\n\n"
            "PRO imkoniyatlari:\n"
            "• Kanal obunasiz foydalanish\n"
            "• Instagram video havolalarini yuklash\n"
            "• Instagram videodan MP3 olish\n",
            reply_markup=plans_keyboard()
        )
        return

    if action == "status":
        if is_pro(uid):
            await message.answer(
                f"⭐ PRO faol!\nMuddati: {pro_until_text(uid)}"
            )
        else:
            await message.answer(tr(lang, "proexpired"))
        return

    if action == "help":
        await message.answer(
            "ℹ️ <b>ASAVED yordam</b>\n\n"
            "🎥 Dumaloq video — videoni dumaloq qiladi.\n"
            "📱 9:16 — vertikal formatga o‘tkazadi.\n"
            "🔗 Video havolasi — qo‘llab-quvvatlanadigan ochiq videolar.\n"
            "⭐ PRO — qo‘shimcha imkoniyatlar.\n\n"
            "Faqat yuklab olishga ruxsatingiz bor kontentdan foydalaning."
        )
        return

    await message.answer(
        tr(lang, "unknown"),
        reply_markup=main_keyboard(lang)
    )


# ================= WEBHOOK =================

async def health(request):
    return web.json_response({
        "status": "ok",
        "bot": "ASAVED"
    })


async def webhook_handler(request):
    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )

    if secret != WEBHOOK_SECRET:
        return web.Response(status=401, text="Unauthorized")

    try:
        data = await request.json()
        update = Update.model_validate(
            data,
            context={"bot": bot}
        )
        await dp.feed_update(bot, update)
        return web.Response(text="OK")
    except Exception:
        log.exception("Webhook update error")
        return web.Response(status=500, text="Update error")


async def on_startup(app):
    init_db()

    if not RENDER_URL:
        raise RuntimeError(
            "Render Environment ichida RENDER_EXTERNAL_URL topilmadi"
        )

    webhook_url = f"{RENDER_URL}/{WEBHOOK_PATH}"

    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=False
    )

    log.info("ASAVED webhook started: %s", webhook_url)


async def on_shutdown(app):
    log.info("ASAVED shutting down")
    await bot.session.close()


def create_app():
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post(f"/{WEBHOOK_PATH}", webhook_handler)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_shutdown)

    return app


if __name__ == "__main__":
    init_db()
    application = create_app()
    web.run_app(
        application,
        host="0.0.0.0",
        port=PORT
    )
