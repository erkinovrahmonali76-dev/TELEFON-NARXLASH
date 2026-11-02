
import asyncio
import logging
import os
import re
import secrets
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, Update, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, FSInputFile
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode


# ==================================================
# 1. SOZLAMALAR
# ==================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = 8461889678
CHANNEL = "@akoshsaved"

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook").strip("/")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

DB_PATH = os.getenv("DB_PATH", "/data/asaved.sqlite3")

MAX_UPLOAD_SIZE = 20 * 1024 * 1024
MAX_DOWNLOAD_SIZE = 48 * 1024 * 1024

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN Render Environment Variables ichida yo'q")

if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
    raise RuntimeError("WEBHOOK_SECRET kamida 16 ta belgidan iborat bo'lishi kerak")

if not re.fullmatch(r"[A-Za-z0-9_-]+", WEBHOOK_PATH):
    raise RuntimeError("WEBHOOK_PATH faqat harf, raqam, _ va - dan iborat bo'lsin")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("ASAVED")

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()
router = Router()
dp.include_router(router)

# Foydalanuvchi tanlagan amallar
user_modes = {}

# Video yuborilgandan keyingi MP3 tugmasi uchun vaqtinchalik havolalar
video_links = {}


# ==================================================
# 2. DATABASE
# ==================================================

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
                user_id INTEGER NOT NULL,
                days INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)

        conn.commit()


def ensure_user(uid):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(user_id, lang) VALUES(?, 'uz')",
            (uid,)
        )
        conn.commit()


def user_row(uid):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id=?",
            (uid,)
        ).fetchone()

        return dict(row) if row else None


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
            (user_id, days, amount, file_id, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (
            uid,
            days,
            amount,
            file_id,
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
                old_expiry = datetime.fromisoformat(row["pro_until"])

                if old_expiry > now:
                    start = old_expiry
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


# ==================================================
# 3. TILLAR
# ==================================================

TEXT = {
    "uz": {
        "menu": "ASAVED botiga xush kelibsiz! Kerakli xizmatni tanlang.",
        "choose": "Tilni tanlang:",
        "round": "🎥 Dumaloq video",
        "format": "📱 9:16 format",
        "link": "🔗 Video havolasi",
        "pro": "⭐ PRO sotib olish",
        "status": "👤 PRO holati",
        "help": "ℹ️ Yordam",
        "roundask": "Dumaloq qilmoqchi bo'lgan videoni yuboring.",
        "formatask": "9:16 formatga o'tkazish uchun videoni yuboring.",
        "linkask": "TikTok, Instagram yoki YouTube havolasini yuboring.",
        "join": "Botdan foydalanish uchun kanalga obuna bo'ling.",
        "check": "✅ Obunani tekshirish",
        "subok": "Obuna tasdiqlandi!",
        "subno": "Avval kanalga obuna bo'ling.",
        "rights": "Faqat yuklab olishga ruxsatingiz bor videolardan foydalaning.",
        "badurl": "Havola noto'g'ri. To'g'ri video havolasini yuboring.",
        "notpro": "⭐ MP3 yuklab olish faqat PRO foydalanuvchilar uchun!",
        "receipt": "Chekingiz adminga yuborildi. Tasdiqlanishini kuting.",
        "wait": "To'lov chekingizni rasm yoki PDF shaklida yuboring.",
        "approved": "✅ To'lov tasdiqlandi! PRO faollashtirildi.",
        "rejected": "❌ To'lov chekingiz rad etildi.",
        "unknown": "Kerakli tugmani tanlang.",
        "fail": "❌ Video bilan ishlashda xatolik yuz berdi.",
        "too_big": "❌ Video hajmi juda katta.",
        "round_done": "✅ Dumaloq video tayyor!",
        "format_done": "✅ 9:16 video tayyor!",
        "proactive": "⭐ PRO faol",
        "proexpired": "PRO faol emas.",
        "audioask": "MP3 tugmasi videoning tagida chiqadi.",
    },

    "ru": {
        "menu": "Добро пожаловать в ASAVED! Выберите функцию.",
        "choose": "Выберите язык:",
        "round": "🎥 Круглое видео",
        "format": "📱 Формат 9:16",
        "link": "🔗 Ссылка на видео",
        "pro": "⭐ Купить PRO",
        "status": "👤 Статус PRO",
        "help": "ℹ️ Помощь",
        "roundask": "Отправьте видео для круглого формата.",
        "formatask": "Отправьте видео для формата 9:16.",
        "linkask": "Отправьте ссылку TikTok, Instagram или YouTube.",
        "join": "Подпишитесь на канал для использования бота.",
        "check": "✅ Проверить подписку",
        "subok": "Подписка подтверждена!",
        "subno": "Сначала подпишитесь на канал.",
        "rights": "Используйте только видео, которые разрешено скачивать.",
        "badurl": "Неверная ссылка.",
        "notpro": "⭐ MP3 доступен только пользователям PRO!",
        "receipt": "Чек отправлен администратору.",
        "wait": "Отправьте чек фото или PDF.",
        "approved": "✅ Оплата подтверждена! PRO активирован.",
        "rejected": "❌ Платёж отклонён.",
        "unknown": "Выберите кнопку.",
        "fail": "❌ Ошибка обработки видео.",
        "too_big": "❌ Видео слишком большое.",
        "round_done": "✅ Круглое видео готово!",
        "format_done": "✅ Видео 9:16 готово!",
        "proactive": "⭐ PRO активен",
        "proexpired": "PRO не активен.",
        "audioask": "Кнопка MP3 находится под видео.",
    },

    "en": {
        "menu": "Welcome to ASAVED! Choose a service.",
        "choose": "Choose your language:",
        "round": "🎥 Round video",
        "format": "📱 9:16 format",
        "link": "🔗 Video link",
        "pro": "⭐ Buy PRO",
        "status": "👤 PRO status",
        "help": "ℹ️ Help",
        "roundask": "Send a video to make it round.",
        "formatask": "Send a video for 9:16 format.",
        "linkask": "Send a TikTok, Instagram or YouTube link.",
        "join": "Subscribe to the channel to use the bot.",
        "check": "✅ Check subscription",
        "subok": "Subscription confirmed!",
        "subno": "Please subscribe first.",
        "rights": "Only use videos you have permission to download.",
        "badurl": "Invalid video link.",
        "notpro": "⭐ MP3 download is only for PRO users!",
        "receipt": "Your receipt was sent to the admin.",
        "wait": "Send your receipt as a photo or PDF.",
        "approved": "✅ Payment approved! PRO activated.",
        "rejected": "❌ Payment rejected.",
        "unknown": "Choose a button.",
        "fail": "❌ Video processing failed.",
        "too_big": "❌ Video is too large.",
        "round_done": "✅ Round video is ready!",
        "format_done": "✅ 9:16 video is ready!",
        "proactive": "⭐ PRO active",
        "proexpired": "PRO inactive.",
        "audioask": "The MP3 button appears below the video.",
    }
}


def tr(lang, key):
    return TEXT.get(lang, TEXT["uz"]).get(
        key, TEXT["uz"].get(key, key)
    )


def get_lang(uid):
    row = user_row(uid)
    return row["lang"] if row and row["lang"] in TEXT else "uz"


# ==================================================
# 4. KLAVIATURALAR
# ==================================================

def lang_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🇺🇿 O'zbekcha",
                callback_data="lang:uz"
            ),
            InlineKeyboardButton(
                text="🇷🇺 Русский",
                callback_data="lang:ru"
            )
        ],
        [
            InlineKeyboardButton(
                text="🇬🇧 English",
                callback_data="lang:en"
            )
        ]
    ])


def main_keyboard(lang):
    t = TEXT.get(lang, TEXT["uz"])

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=t["round"]),
                KeyboardButton(text=t["format"])
            ],
            [
                KeyboardButton(text=t["link"])
            ],
            [
                KeyboardButton(text=t["pro"]),
                KeyboardButton(text=t["status"])
            ],
            [
                KeyboardButton(text=t["help"])
            ]
        ],
        resize_keyboard=True
    )


def join_keyboard(lang):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📢 @akoshsaved",
                url="https://t.me/akoshsaved"
            )
        ],
        [
            InlineKeyboardButton(
                text=tr(lang, "check"),
                callback_data="checksub"
            )
        ]
    ])


def plans_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="7 kun — 30 000 so'm",
                callback_data="buy:7:30000"
            )
        ],
        [
            InlineKeyboardButton(
                text="30 kun — 55 000 so'm",
                callback_data="buy:30:55000"
            )
        ]
    ])


# ==================================================
# 5. OBUNA TEKSHIRISH
# ==================================================

async def subscribed(uid):
    try:
        member = await bot.get_chat_member(CHANNEL, uid)
        return member.status in (
            "member", "administrator", "creator"
        )
    except Exception:
        log.exception("Channel membership check failed")
        return False


async def require_access(message, lang):
    uid = message.from_user.id

    if is_pro(uid):
        return True

    if await subscribed(uid):
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


# ==================================================
# 6. START VA TIL
# ==================================================

@router.message(CommandStart())
async def start_handler(message: Message):
    uid = message.from_user.id
    ensure_user(uid)

    row = user_row(uid)

    if not row or not row.get("lang"):
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

    if lang not in TEXT:
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
    lang = get_lang(uid)

    if is_pro(uid) or await subscribed(uid):
        await call.answer(
            tr(lang, "subok"),
            show_alert=True
        )

        await call.message.answer(
            tr(lang, "menu"),
            reply_markup=main_keyboard(lang)
        )
    else:
        await call.answer(
            tr(lang, "subno"),
            show_alert=True
        )


# ==================================================
# 7. PRO SOTIB OLISH
# ==================================================

@router.callback_query(F.data.startswith("buy:"))
async def buy_plan(call: CallbackQuery):
    uid = call.from_user.id

    try:
        _, days, amount = call.data.split(":")
        days = int(days)
        amount = int(amount)
    except Exception:
        await call.answer("Tarif xatosi", show_alert=True)
        return

    if (days, amount) not in (
        (7, 30000),
        (30, 55000)
    ):
        await call.answer("Noto'g'ri tarif", show_alert=True)
        return

    card = os.getenv("PAYMENT_CARD", "").strip()
    holder = os.getenv("PAYMENT_HOLDER", "").strip()

    if not card or not holder:
        await call.answer(
            "To'lov sozlamalari tayyor emas.",
            show_alert=True
        )
        return

    user_modes[uid] = f"receipt:{days}:{amount}"

    await call.message.answer(
        "⭐ <b>ASAVED PRO</b>\n\n"
        f"📦 Tarif: {days} kun\n"
        f"💰 Narxi: {amount:,} so'm\n\n"
        f"💳 Karta: <code>{card}</code>\n"
        f"👤 Karta egasi: {holder}\n\n"
        "To'lovdan so'ng chekni rasm yoki PDF shaklida yuboring."
        .replace(",", " ")
    )

    await call.answer()


# ==================================================
# 8. ADMIN PANEL
# ==================================================

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
        "🛠 <b>ASAVED ADMIN</b>\n\n"
        f"👥 Foydalanuvchilar: {users}\n"
        f"⭐ Faol PRO: {active}\n"
        f"⏳ Kutilayotgan chek: {pending}"
    )


@router.callback_query(F.data.startswith("payok:"))
async def approve_callback(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Ruxsat yo'q", show_alert=True)
        return

    try:
        pid = int(call.data.split(":")[1])
    except Exception:
        await call.answer("Chek ID xato", show_alert=True)
        return

    result = approve_payment(pid)

    if not result:
        await call.answer(
            "Bu chek avval ko'rib chiqilgan.",
            show_alert=True
        )
        return

    uid, expiry = result
    lang = get_lang(uid)

    try:
        await bot.send_message(
            uid,
            tr(lang, "approved") +
            f"\nMuddati: {expiry.strftime('%d.%m.%Y %H:%M')}"
        )
    except Exception:
        log.exception("Approval notification failed")

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
        await call.answer("Ruxsat yo'q", show_alert=True)
        return

    try:
        pid = int(call.data.split(":")[1])
    except Exception:
        await call.answer("Chek ID xato", show_alert=True)
        return

    uid = reject_payment(pid)

    if not uid:
        await call.answer(
            "Bu chek avval ko'rib chiqilgan.",
            show_alert=True
        )
        return

    lang = get_lang(uid)

    try:
        await bot.send_message(uid, tr(lang, "rejected"))
    except Exception:
        log.exception("Rejection notification failed")

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

    await call.answer("Chek rad etildi.")


# ==================================================
# 9. VIDEO FORMATLASH
# ==================================================

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
        "ffmpeg", "-y",
        "-i", str(input_path),
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
        log.error(
            "FFmpeg error: %s",
            stderr.decode(errors="ignore")[-2000:]
        )
        raise RuntimeError("FFmpeg failed")


async def process_uploaded(message, mode, lang):
    video = message.video

    if not video:
        await message.answer(
            "Videoni oddiy video sifatida yuboring."
        )
        return

    if video.file_size and video.file_size > MAX_UPLOAD_SIZE:
        await message.answer(tr(lang, "too_big"))
        return

    status = await message.answer("⏳ Video tayyorlanmoqda...")

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "input.mp4"
        output = Path(tmp) / "output.mp4"

        try:
            telegram_file = await bot.get_file(video.file_id)

            await bot.download_file(
                telegram_file.file_path,
                destination=source
            )

            await run_ffmpeg(source, output, mode)

            if mode == "round":
                await message.answer_video_note(
                    video_note=FSInputFile(output)
                )
                await status.edit_text(tr(lang, "round_done"))
            else:
                await message.answer_video(
                    video=FSInputFile(output),
                    caption=tr(lang, "format_done")
                )
                await status.edit_text("✅ Tayyor!")

        except Exception:
            log.exception("Uploaded video conversion failed")

            try:
                await status.edit_text(tr(lang, "fail"))
            except Exception:
                pass


@router.message(F.video)
async def video_handler(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    lang = get_lang(uid)
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
    await message.answer(
        "Bu video allaqachon dumaloq formatda."
    )


# ==================================================
# 10. HAVOLA TEKSHIRISH VA YUKLASH
# ==================================================

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

    if audio:
        candidates = list(folder.glob("*.mp3"))
    else:
        candidates = [
            p for p in folder.iterdir()
            if p.is_file()
            and p.suffix.lower() not in (".part", ".ytdl")
        ]

    if not candidates:
        raise RuntimeError("Downloaded file not found")

    result = max(
        candidates,
        key=lambda p: p.stat().st_size
    )

    if result.stat().st_size > MAX_DOWNLOAD_SIZE:
        raise RuntimeError("Downloaded file too large")

    return result


async def process_link_message(message, lang):
    url = (message.text or "").strip()

    if not valid_video_url(url):
        await message.answer(tr(lang, "badurl"))
        return

    if not await require_access(message, lang):
        return

    await message.answer(tr(lang, "rights"))
    status = await message.answer("⏳ Video yuklanmoqda...")

    with tempfile.TemporaryDirectory() as tmp:
        try:
            path = await asyncio.to_thread(
                download_media_sync,
                url,
                tmp,
                False
            )

            # MP3 tugmasi uchun qisqa vaqtinchalik kalit
            token = secrets.token_hex(4)
            video_links[token] = url

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🎵 MP3 yuklab olish",
                        callback_data=f"mp3:{token}"
                    )
                ]]
            )

            await message.answer_video(
                video=FSInputFile(path),
                caption="✅ Video tayyor!",
                reply_markup=keyboard
            )

            await status.edit_text("✅ Video yuborildi!")

        except Exception:
            log.exception("Video link download failed")

            try:
                await status.edit_text(
                    "❌ Video yuklanmadi. Havolani tekshiring. "
                    "Ba'zi videolar platforma cheklovlari sabab ishlamasligi mumkin."
                )
            except Exception:
                pass


# ==================================================
# 11. VIDEO TAGIDAGI MP3 TUGMASI
# ==================================================

@router.callback_query(F.data.startswith("mp3:"))
async def mp3_callback(call: CallbackQuery):
    uid = call.from_user.id

    # PRO bo'lmasa, shu yerda to'xtaydi
    if not is_pro(uid):
        await call.answer(
            "⭐ MP3 yuklab olish faqat PRO uchun!",
            show_alert=True
        )
        return

    token = call.data.split(":", 1)[1]
    url = video_links.get(token)

    if not url:
        await call.answer(
            "Havola eskirgan. Videoni qaytadan yuboring.",
            show_alert=True
        )
        return

    await call.answer("MP3 tayyorlanmoqda...")

    status = await call.message.answer(
        "🎵 Musiqa MP3 shaklida tayyorlanmoqda..."
    )

    with tempfile.TemporaryDirectory() as tmp:
        try:
            path = await asyncio.to_thread(
                download_media_sync,
                url,
                tmp,
                True
            )

            await call.message.answer_audio(
                audio=FSInputFile(path),
                caption="🎵 MP3 tayyor!"
            )

            await status.edit_text("✅ MP3 yuborildi!")

        except Exception:
            log.exception("MP3 download failed")

            try:
                await status.edit_text(
                    "❌ MP3 olishda xatolik. Bu video uchun audio mavjud bo'lmasligi mumkin."
                )
            except Exception:
                pass


# ==================================================
# 12. TO'LOV CHEKI
# ==================================================

@router.message(F.photo | F.document)
async def receipt_handler(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    lang = get_lang(uid)
    mode = user_modes.get(uid)

    if not (
        isinstance(mode, str)
        and mode.startswith("receipt:")
    ):
        await message.answer(tr(lang, "unknown"))
        return

    try:
        _, days, amount = mode.split(":")
        days = int(days)
        amount = int(amount)
    except Exception:
        await message.answer(
            "❌ To'lov ma'lumotlarida xatolik."
        )
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        is_photo = True

    elif message.document:
        document = message.document

        if document.mime_type != "application/pdf":
            await message.answer(
                "Chekni rasm yoki PDF ko'rinishida yuboring."
            )
            return

        file_id = document.file_id
        is_photo = False

    else:
        return

    try:
        pid = add_payment(
            uid,
            days,
            amount,
            file_id
        )
    except Exception:
        log.exception("Payment database error")
        await message.answer(
            "❌ Chekni saqlashda xatolik."
        )
        return

    plan = f"{days} kun / {amount:,} so'm".replace(",", " ")

    caption = (
        "💳 <b>YANGI PRO TO'LOV</b>\n\n"
        f"👤 User ID: <code>{uid}</code>\n"
        f"📦 Tarif: {plan}\n"
        f"🧾 Chek ID: {pid}\n\n"
        "To'lovni tekshiring:"
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
        log.exception("Admin receipt delivery failed")

        reject_payment(pid)

        await message.answer(
            "❌ Chek adminga yuborilmadi. "
            "Admin botga /start bosganini tekshiring."
        )
        return

    user_modes.pop(uid, None)

    await message.answer(tr(lang, "receipt"))


# ==================================================
# 13. MATNLI XABARLAR
# ==================================================

@router.message(F.text)
async def text_router(message: Message):
    uid = message.from_user.id
    ensure_user(uid)

    lang = get_lang(uid)
    text = message.text.strip()
    mode = user_modes.get(uid)

    # Foydalanuvchi havolani to'g'ridan-to'g'ri yuborsa ham ishlaydi
    if valid_video_url(text):
        await process_link_message(message, lang)
        user_modes.pop(uid, None)
        return

    # Chek yuborish rejimida matn kelsa
    if isinstance(mode, str) and mode.startswith("receipt:"):
        await message.answer(tr(lang, "wait"))
        return

    # Video yuborish rejimida
    if mode in ("round", "916"):
        await message.answer(
            "Iltimos, videoni oddiy video sifatida yuboring."
        )
        return

    # Havola tugmasi bosilgan bo'lsa
    if mode == "link":
        await message.answer(tr(lang, "linkask"))
        return

    t = TEXT.get(lang, TEXT["uz"])

    mapping = {
        t["round"]: "round",
        t["format"]: "format",
        t["link"]: "link",
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

    if action == "format":
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

    if action == "pro":
        await message.answer(
            "⭐ <b>ASAVED PRO</b>\n\n"
            "7 kun — 30 000 so'm\n"
            "30 kun — 55 000 so'm\n\n"
            "PRO imkoniyatlari:\n"
            "• Kanal obunasiz foydalanish\n"
            "• Video havolasidan yuklash\n"
            "• Video tagidagi MP3 tugmasidan foydalanish\n",
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
            "ℹ️ <b>ASAVED YORDAM</b>\n\n"
            "🎥 Dumaloq video — videoni dumaloq qiladi.\n"
            "📱 9:16 — vertikal formatga o'tkazadi.\n"
            "🔗 Video havolasi — video yuboradi.\n"
            "🎵 MP3 tugmasi — videoning tagida chiqadi.\n"
            "⭐ MP3 olish uchun PRO kerak.\n\n"
            "Faqat yuklab olishga ruxsatingiz bor videolardan foydalaning."
        )
        return

    await message.answer(
        tr(lang, "unknown"),
        reply_markup=main_keyboard(lang)
    )


# ==================================================
# 14. WEBHOOK VA RENDER
# ==================================================

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
        return web.Response(
            status=401,
            text="Unauthorized"
        )

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
        return web.Response(
            status=500,
            text="Update error"
        )


async def on_startup(app):
    init_db()

    if not RENDER_URL:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL topilmadi. Render Web Service sozlamasini tekshiring."
        )

    webhook_url = f"{RENDER_URL}/{WEBHOOK_PATH}"

    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=False
    )

    log.info("ASAVED webhook started")


async def on_shutdown(app):
    log.info("ASAVED shutting down")
    await bot.session.close()


def create_app():
    app = web.Application(
        client_max_size=2 * 1024 * 1024
    )

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    app.router.add_post(
        f"/{WEBHOOK_PATH}",
        webhook_handler
    )

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
