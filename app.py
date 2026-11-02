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
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("asaved")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook").strip("/")
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

ADMIN_ID = 8461889678
CHANNEL = "@akoshsaved"
CHANNEL_URL = "https://t.me/akoshsaved"
DB_PATH = os.getenv("DB_PATH", "/data/asaved.sqlite3")
PAYMENT_CARD = os.getenv("PAYMENT_CARD", "").strip()
PAYMENT_HOLDER = os.getenv("PAYMENT_HOLDER", "").strip()

MAX_INPUT = 20 * 1024 * 1024
MAX_OUTPUT = 49 * 1024 * 1024
MAX_VIDEO_SECONDS = 600

# Cheklangan parallel video ishlovlari kichik serverni band qilib qo‘ymasligi uchun.
MEDIA_SEMAPHORE = asyncio.Semaphore(2)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN kiritilmagan")
if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET kamida 16 belgi bo‘lishi kerak")
if not BASE_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL topilmadi")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

LANGS = ("uz", "ru", "en")
TEXT = {
 "uz": {
  "choose":"Tilni tanlang:",
  "join":"Botdan foydalanish uchun kanalga obuna bo‘ling, so‘ng tekshiring.",
  "check":"✅ Obunani tekshirish",
  "menu":"ASAVED bosh menyusi",
  "round":"🎥 Dumaloq video",
  "link":"🔗 Video havolasi",
  "format":"📱 9:16 format",
  "pro":"💎 PRO sotib olish",
  "status":"👤 Mening PRO holatim",
  "help":"Kerakli bo‘limni tanlang.",
  "roundask":"Galereyadan 60 soniyagacha video yuboring.",
  "formatask":"9:16 formatga o‘tkazish uchun video yuboring.",
  "linkask":"YouTube, Instagram yoki TikTok video havolasini yuboring.",
  "proactive":"PRO faol",
  "proexpired":"PRO faol emas",
  "prodesc":"💎 <b>ASAVED PRO</b>\n\n7 kun — <b>30 000 so‘m</b>\n30 kun — <b>55 000 so‘m</b>\n\nPRO: Instagram videosini olish va ruxsat etilgan videoning audio qismini alohida olish. PRO foydalanuvchiga kanal obunasi sharti qo‘yilmaydi.",
  "week":"7 kun — 30 000 so‘m",
  "month":"30 kun — 55 000 so‘m",
  "pay":"To‘lovni quyidagi karta orqali amalga oshiring:\n<b>{holder}</b>\n<code>{card}</code>\n\nKeyin to‘lov chekini rasm yoki PDF/fayl qilib shu yerga yuboring. Admin to‘lov tushganini tekshiradi.",
  "receipt":"Chek adminga yuborildi. To‘lov tasdiqlanishini kuting.",
  "wait":"Chek yuborish rejimi faol. To‘lov chekini rasm yoki fayl qilib yuboring.",
  "approved":"🎉 To‘lov tasdiqlandi! PRO {days} kun faollashtirildi.",
  "rejected":"To‘lov cheki admin tomonidan rad etildi. Agar xato bo‘lsa, admin bilan bog‘laning.",
  "notpro":"Bu imkoniyat faqat PRO foydalanuvchilar uchun.",
  "audio":"🎵 Instagram videosining audio qismini olish",
  "video":"🎬 Instagram videosini olish",
  "rights":"Faqat o‘zingizga tegishli yoki yuklab olish va audiosidan foydalanishga ruxsatingiz bor kontentni davom ettiring.",
  "badurl":"Faqat ochiq HTTPS YouTube, Instagram yoki TikTok video havolasini yuboring.",
  "fail":"Videoni olib bo‘lmadi. Havola yopiq bo‘lishi, platforma cheklovi yoki hajm sabab bo‘lishi mumkin.",
  "too_big":"Video 49 MB dan katta. Qisqaroq video tanlang.",
  "too_input":"Telegram orqali olinadigan video 20 MB dan oshmasin.",
  "round_done":"Dumaloq video tayyor!",
  "916_done":"9:16 format tayyor!",
  "admin_only":"Bu buyruq faqat admin uchun.",
  "pending":"Yangi PRO to‘lovi\nFoydalanuvchi: <code>{uid}</code>\nTarif: {plan}\nTo‘lovni bank ilovasida tekshiring.",
  "approved_admin":"To‘lov tasdiqlandi.",
  "rejected_admin":"To‘lov rad etildi.",
  "unknown":"Menyudan bo‘lim tanlang."
 },
 "ru": {
  "choose":"Выберите язык:",
  "join":"Чтобы пользоваться ботом, подпишитесь на канал и нажмите «Проверить».",
  "check":"✅ Проверить подписку",
  "menu":"Главное меню ASAVED",
  "round":"🎥 Круглое видео",
  "link":"🔗 Ссылка на видео",
  "format":"📱 Формат 9:16",
  "pro":"💎 Купить PRO",
  "status":"👤 Мой статус PRO",
  "help":"Выберите нужный раздел.",
  "roundask":"Отправьте видео из галереи длительностью до 60 секунд.",
  "formatask":"Отправьте видео для преобразования в формат 9:16.",
  "linkask":"Отправьте ссылку на видео YouTube, Instagram или TikTok.",
  "proactive":"PRO активен",
  "proexpired":"PRO не активен",
  "prodesc":"💎 <b>ASAVED PRO</b>\n\n7 дней — <b>30 000 сум</b>\n30 дней — <b>55 000 сум</b>\n\nPRO: загрузка Instagram-видео и отдельное извлечение аудио из контента, который разрешено скачивать. Для PRO подписка на канал не обязательна.",
  "week":"7 дней — 30 000 сум",
  "month":"30 дней — 55 000 сум",
  "pay":"Оплатите на карту:\n<b>{holder}</b>\n<code>{card}</code>\n\nЗатем отправьте чек фотографией или файлом. Администратор проверит поступление оплаты.",
  "receipt":"Чек отправлен администратору. Дождитесь подтверждения.",
  "wait":"Отправьте чек оплаты фотографией или файлом.",
  "approved":"🎉 Оплата подтверждена! PRO активирован на {days} дней.",
  "rejected":"Чек отклонён администратором. Если это ошибка, свяжитесь с администратором.",
  "notpro":"Эта функция доступна только PRO.",
  "audio":"🎵 Извлечь аудио из Instagram-видео",
  "video":"🎬 Скачать Instagram-видео",
  "rights":"Продолжайте только с контентом, который принадлежит вам или на скачивание и использование аудио которого у вас есть разрешение.",
  "badurl":"Отправьте открытую HTTPS-ссылку YouTube, Instagram или TikTok.",
  "fail":"Не удалось получить видео. Возможно, ссылка закрыта или действуют ограничения платформы.",
  "too_big":"Видео больше 49 МБ. Выберите более короткое видео.",
  "too_input":"Размер видео для загрузки через Telegram не должен превышать 20 МБ.",
  "round_done":"Круглое видео готово!",
  "916_done":"Формат 9:16 готов!",
  "admin_only":"Команда доступна только администратору.",
  "pending":"Новый PRO-платёж\nПользователь: <code>{uid}</code>\nТариф: {plan}\nПроверьте поступление оплаты в банковском приложении.",
  "approved_admin":"Оплата подтверждена.",
  "rejected_admin":"Оплата отклонена.",
  "unknown":"Выберите раздел в меню."
 },
 "en": {
  "choose":"Choose your language:",
  "join":"Subscribe to the channel to use the bot, then press Check subscription.",
  "check":"✅ Check subscription",
  "menu":"ASAVED main menu",
  "round":"🎥 Round video",
  "link":"🔗 Video link",
  "format":"📱 9:16 format",
  "pro":"💎 Buy PRO",
  "status":"👤 My PRO status",
  "help":"Choose an option.",
  "roundask":"Send a gallery video up to 60 seconds.",
  "formatask":"Send a video to convert it to 9:16.",
  "linkask":"Send a YouTube, Instagram, or TikTok video link.",
  "proactive":"PRO is active",
  "proexpired":"PRO is not active",
  "prodesc":"💎 <b>ASAVED PRO</b>\n\n7 days — <b>30,000 UZS</b>\n30 days — <b>55,000 UZS</b>\n\nPRO: download Instagram videos and extract audio separately from content you are allowed to download. PRO users do not need to subscribe to the channel.",
  "week":"7 days — 30,000 UZS",
  "month":"30 days — 55,000 UZS",
  "pay":"Pay using this card:\n<b>{holder}</b>\n<code>{card}</code>\n\nThen send the receipt as a photo or file. The admin will verify the payment.",
  "receipt":"Your receipt was sent to the admin. Please wait for approval.",
  "wait":"Send the payment receipt as a photo or file.",
  "approved":"🎉 Payment approved! PRO activated for {days} days.",
  "rejected":"The admin rejected the receipt. Contact the admin if you think this is a mistake.",
  "notpro":"This feature is available to PRO users only.",
  "audio":"🎵 Extract audio from Instagram video",
  "video":"🎬 Download Instagram video",
  "rights":"Continue only with content you own or have permission to download and use its audio.",
  "badurl":"Send a public HTTPS YouTube, Instagram, or TikTok video link.",
  "fail":"Could not download the video. The link may be private or restricted by the platform.",
  "too_big":"The video is larger than 49 MB. Choose a shorter video.",
  "too_input":"Videos downloaded through Telegram must be no larger than 20 MB.",
  "round_done":"Round video is ready!",
  "916_done":"9:16 video is ready!",
  "admin_only":"This command is for the admin only.",
  "pending":"New PRO payment\nUser: <code>{uid}</code>\nPlan: {plan}\nVerify that the payment arrived in the banking app.",
  "approved_admin":"Payment approved.",
  "rejected_admin":"Payment rejected.",
  "unknown":"Choose an option from the menu."
 }
}

def tr(lang, key, **kwargs):
    return TEXT.get(lang, TEXT["uz"]).get(key, TEXT["uz"].get(key, key)).format(**kwargs)

def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, lang TEXT, pro_until TEXT, created_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            days INTEGER NOT NULL, amount INTEGER NOT NULL, status TEXT NOT NULL,
            receipt_file_id TEXT, created_at TEXT NOT NULL
        )""")

def user_row(uid):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def set_lang(uid, lang):
    with db() as c:
        c.execute("""INSERT INTO users(user_id,lang,created_at) VALUES(?,?,?)
          ON CONFLICT(user_id) DO UPDATE SET lang=excluded.lang""",
          (uid, lang, datetime.now(timezone.utc).isoformat()))

def is_pro(uid):
    row = user_row(uid)
    if not row or not row["pro_until"]:
        return False
    try:
        return datetime.fromisoformat(row["pro_until"]) > datetime.now(timezone.utc)
    except Exception:
        return False

def pro_until_text(uid):
    row = user_row(uid)
    return row["pro_until"] if row else None

def add_payment(uid, days, amount, file_id):
    with db() as c:
        cur = c.execute("""INSERT INTO payments(user_id,days,amount,status,receipt_file_id,created_at)
          VALUES(?,?,?,'pending',?,?)""",
          (uid, days, amount, file_id, datetime.now(timezone.utc).isoformat()))
        return cur.lastrowid

def approve_payment(pid):
    with db() as c:
        p = c.execute("SELECT * FROM payments WHERE id=? AND status='pending'", (pid,)).fetchone()
        if not p:
            return None
        row = c.execute("SELECT pro_until FROM users WHERE user_id=?", (p["user_id"],)).fetchone()
        now = datetime.now(timezone.utc)
        start = now
        if row and row["pro_until"]:
            try:
                old = datetime.fromisoformat(row["pro_until"])
                if old > now:
                    start = old
            except Exception:
                pass
        expiry = start + timedelta(days=int(p["days"]))
        c.execute("UPDATE payments SET status='approved' WHERE id=?", (pid,))
        c.execute("""INSERT INTO users(user_id,pro_until,created_at) VALUES(?,?,?)
          ON CONFLICT(user_id) DO UPDATE SET pro_until=excluded.pro_until""",
          (p["user_id"], expiry.isoformat(), now.isoformat()))
        return dict(p), expiry

def reject_payment(pid):
    with db() as c:
        p = c.execute("SELECT * FROM payments WHERE id=? AND status='pending'", (pid,)).fetchone()
        if not p:
            return None
        c.execute("UPDATE payments SET status='rejected' WHERE id=?", (pid,))
        return dict(p)

def main_keyboard(lang, pro=False):
    rows = [
      [KeyboardButton(text=tr(lang,"round")), KeyboardButton(text=tr(lang,"format"))],
      [KeyboardButton(text=tr(lang,"link"))],
      [KeyboardButton(text=tr(lang,"pro")), KeyboardButton(text=tr(lang,"status"))],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def lang_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
      [InlineKeyboardButton(text="🇺🇿 O‘zbekcha", callback_data="lang:uz")],
      [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru")],
      [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en")]
    ])

def join_keyboard(lang):
    return InlineKeyboardMarkup(inline_keyboard=[
      [InlineKeyboardButton(text="📢 @akoshsaved", url=CHANNEL_URL)],
      [InlineKeyboardButton(text=tr(lang,"check"), callback_data="checksub")]
    ])

async def subscribed(uid):
    try:
        member = await bot.get_chat_member(CHANNEL, uid)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        log.exception("Channel membership check failed")
        return False

async def access_ok(uid):
    return is_pro(uid) or await subscribed(uid)

async def show_menu(message: Message, lang):
    await message.answer(tr(lang,"menu") + "\n\n" + tr(lang,"help"),
                         reply_markup=main_keyboard(lang, is_pro(message.from_user.id)))

async def require_access(message: Message, lang):
    if is_pro(message.from_user.id):
        return True
    if not await subscribed(message.from_user.id):
        await message.answer(tr(lang,"join"), reply_markup=join_keyboard(lang))
        return False
    return True

user_modes = {}

@router.message(CommandStart())
async def start(message: Message):
    row = user_row(message.from_user.id)
    if not row or not row["lang"]:
        await message.answer("ASAVED — " + " / ".join(TEXT[x]["choose"] for x in ("uz","ru","en")),
                             reply_markup=lang_keyboard())
        return
    lang = row["lang"]
    if not await require_access(message, lang):
        return
    await show_menu(message, lang)

@router.callback_query(F.data.startswith("lang:"))
async def choose_lang(call: CallbackQuery):
    lang = call.data.split(":",1)[1]
    if lang not in LANGS:
        await call.answer()
        return
    set_lang(call.from_user.id, lang)
    await call.message.edit_reply_markup(reply_markup=None)
    if not is_pro(call.from_user.id) and not await subscribed(call.from_user.id):
        await call.message.answer(tr(lang,"join"), reply_markup=join_keyboard(lang))
    else:
        await call.message.answer(tr(lang,"menu"), reply_markup=main_keyboard(lang, is_pro(call.from_user.id)))
    await call.answer()

@router.callback_query(F.data == "checksub")
async def check_sub(call: CallbackQuery):
    row = user_row(call.from_user.id)
    lang = row["lang"] if row and row["lang"] else "uz"
    if is_pro(call.from_user.id) or await subscribed(call.from_user.id):
        await call.message.answer(tr(lang,"menu"), reply_markup=main_keyboard(lang, is_pro(call.from_user.id)))
        await call.answer("OK")
    else:
        await call.answer("Kanalga obuna bo‘ling / Подпишитесь / Subscribe", show_alert=True)

@router.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer(tr("uz","admin_only"))
        return
    with db() as c:
        pending = c.execute("SELECT COUNT(*) n FROM payments WHERE status='pending'").fetchone()["n"]
        active = c.execute("SELECT COUNT(*) n FROM users WHERE pro_until > ?", (datetime.now(timezone.utc).isoformat(),)).fetchone()["n"]
    await message.answer(f"🛠 <b>ASAVED Admin</b>\nKutilayotgan to‘lovlar: {pending}\nFaol PRO: {active}")

@router.message(F.text)
async def text_router(message: Message):
    row = user_row(message.from_user.id)
    lang = row["lang"] if row and row["lang"] else "uz"
    text = (message.text or "").strip()
    if user_modes.get(message.from_user.id) == "link":
        url = text
        if not valid_url(url):
            await message.answer(tr(lang,"badurl"))
            return
        if not await require_access(message, lang):
            return
        buttons = [[InlineKeyboardButton(text=tr(lang,"video"), callback_data="linkvideo")]]
        if is_pro(message.from_user.id) and "instagram.com" in (urlparse(url).hostname or ""):
            buttons.append([InlineKeyboardButton(text=tr(lang,"audio"), callback_data="linkaudio")])
        buttons.append([InlineKeyboardButton(text="↩️ Bekor qilish", callback_data="linkcancel")])
        user_modes[message.from_user.id] = "link_choice:" + url
        await message.answer(tr(lang,"rights"), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        return
    if user_modes.get(message.from_user.id) == "receipt":
        await message.answer(tr(lang,"wait"))
        return
    mapping = {
      tr(lang,"round"):"round", tr(lang,"format"):"916", tr(lang,"link"):"link",
      tr(lang,"pro"):"pro", tr(lang,"status"):"status"
    }
    action = mapping.get(text)
    if not action:
        await message.answer(tr(lang,"unknown"), reply_markup=main_keyboard(lang, is_pro(message.from_user.id)))
        return
    if action != "pro" and action != "status":
        if not await require_access(message, lang):
            return
    if action == "round":
        user_modes[message.from_user.id] = "round"
        await message.answer(tr(lang,"roundask"))
    elif action == "916":
        user_modes[message.from_user.id] = "916"
        await message.answer(tr(lang,"formatask"))
    elif action == "link":
        user_modes[message.from_user.id] = "link"
        await message.answer(tr(lang,"linkask"))
    elif action == "pro":
        await message.answer(tr(lang,"prodesc"), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
          [InlineKeyboardButton(text=tr(lang,"week"), callback_data="buy:7")],
          [InlineKeyboardButton(text=tr(lang,"month"), callback_data="buy:30")]
        ]))
    elif action == "status":
        expiry = pro_until_text(message.from_user.id)
        if is_pro(message.from_user.id):
            await message.answer(f"💎 {tr(lang,'proactive')}\n{expiry}")
        else:
            await message.answer(tr(lang,"proexpired"))

@router.callback_query(F.data.startswith("buy:"))
async def buy_plan(call: CallbackQuery):
    langrow = user_row(call.from_user.id)
    lang = langrow["lang"] if langrow and langrow["lang"] else "uz"
    days = int(call.data.split(":")[1])
    if days not in (7,30):
        await call.answer()
        return
    amount = 30000 if days == 7 else 55000
    if not PAYMENT_CARD or not PAYMENT_HOLDER:
        await call.answer("To‘lov sozlamalari admin tomonidan kiritilmagan.", show_alert=True)
        return
    user_modes[call.from_user.id] = f"receipt:{days}:{amount}"
    await call.message.answer(tr(lang,"pay", holder=PAYMENT_HOLDER, card=PAYMENT_CARD))
    await call.message.answer(tr(lang,"wait"))
    await call.answer()

@router.message(F.photo | F.document)
async def receipt_or_video(message: Message):
    uid = message.from_user.id
    mode = user_modes.get(uid)
    row = user_row(uid)
    lang = row["lang"] if row and row["lang"] else "uz"
    if isinstance(mode, str) and mode.startswith("receipt:"):
        _, days, amount = mode.split(":")
        file_id = message.photo[-1].file_id if message.photo else message.document.file_id
        pid = add_payment(uid, int(days), int(amount), file_id)
        user_modes.pop(uid, None)
        await message.answer(tr(lang,"receipt"), reply_markup=main_keyboard(lang, is_pro(uid)))
        plan = f"{days} kun / {amount:,} so‘m".replace(",", " ")
        admin_text = tr("uz","pending", uid=uid, plan=plan)
        await bot.send_message(ADMIN_ID, admin_text,
          reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"payok:{pid}"),
             InlineKeyboardButton(text="❌ Rad etish", callback_data=f"payno:{pid}")]
          ]))
        try:
            await bot.copy_message(ADMIN_ID, message.chat.id, message.message_id)
        except Exception:
            log.exception("Receipt forwarding failed")
        return
    if mode in ("round","916"):
        await process_uploaded(message, mode, lang)
        return
    await message.answer(tr(lang,"unknown"), reply_markup=main_keyboard(lang, is_pro(uid)))

@router.callback_query(F.data.startswith("payok:"))
async def admin_approve(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Admin emas", show_alert=True)
        return
    pid = int(call.data.split(":")[1])
    result = approve_payment(pid)
    if not result:
        await call.answer("To‘lov allaqachon ko‘rib chiqilgan.", show_alert=True)
        return
    payment, expiry = result
    u = user_row(payment["user_id"])
    lang = u["lang"] if u and u["lang"] else "uz"
    await bot.send_message(payment["user_id"], tr(lang,"approved", days=payment["days"]),
                           reply_markup=main_keyboard(lang, True))
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(tr("uz","approved_admin"))
    await call.answer("PRO yoqildi")

@router.callback_query(F.data.startswith("payno:"))
async def admin_reject(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Admin emas", show_alert=True)
        return
    pid = int(call.data.split(":")[1])
    payment = reject_payment(pid)
    if not payment:
        await call.answer("To‘lov allaqachon ko‘rib chiqilgan.", show_alert=True)
        return
    u = user_row(payment["user_id"])
    lang = u["lang"] if u and u["lang"] else "uz"
    await bot.send_message(payment["user_id"], tr(lang,"rejected"))
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(tr("uz","rejected_admin"))
    await call.answer("Rad etildi")

def video_info(message):
    if message.video:
        return message.video.file_id, message.video.file_size, message.video.duration
    if message.document and (message.document.mime_type or "").startswith("video/"):
        return message.document.file_id, message.document.file_size, None
    return None

async def get_telegram_video(message, dst):
    info = video_info(message)
    if not info:
        raise ValueError("Video fayl yuboring.")
    fid, size, duration = info
    if size and size > MAX_INPUT:
        raise ValueError("Telegram orqali yuklanadigan video 20 MB dan oshmasin.")
    if duration and duration > MAX_VIDEO_SECONDS:
        raise ValueError("Video 10 daqiqadan uzun.")
    f = await bot.get_file(fid)
    await bot.download_file(f.file_path, destination=dst)

async def ffmpeg(args):
    async with MEDIA_SEMAPHORE:
        p = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-threads", "2", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, err = await p.communicate()
        if p.returncode:
            raise RuntimeError(err.decode("utf-8", "ignore")[-700:] or "FFmpeg xatosi")

async def process_uploaded(message, mode, lang):
    status = await message.answer("⏳ Video tayyorlanmoqda...")
    try:
        with tempfile.TemporaryDirectory(prefix="asaved_") as td:
            src, out = Path(td)/"input.mp4", Path(td)/"output.mp4"
            await get_telegram_video(message, src)
            if mode == "round":
                await ffmpeg(["-i",str(src),"-t","60","-vf",
                    "scale=480:480:force_original_aspect_ratio=increase,crop=480:480,setsar=1",
                    "-c:v","libx264","-preset","ultrafast","-crf","28","-threads","2","-c:a","aac","-b:a","80k",
                    "-movflags","+faststart",str(out)])
                if out.stat().st_size > MAX_OUTPUT:
                    raise ValueError(tr(lang,"too_big"))
                await message.answer_video_note(FSInputFile(out))
            else:
                await ffmpeg(["-i",str(src),"-vf",
                    "scale=540:960:force_original_aspect_ratio=increase,crop=540:960,setsar=1",
                    "-c:v","libx264","-preset","ultrafast","-crf","27","-threads","2","-c:a","aac","-b:a","96k",
                    "-movflags","+faststart",str(out)])
                if out.stat().st_size > MAX_OUTPUT:
                    raise ValueError(tr(lang,"too_big"))
                await message.answer_video(FSInputFile(out),caption=tr(lang,"916_done"))
        await status.delete()
    except Exception as e:
        log.exception("Upload video processing failed")
        await status.edit_text("❌ " + str(e)[:500])
    finally:
        user_modes.pop(message.from_user.id, None)

ALLOWED_HOSTS = {
 "youtube.com","www.youtube.com","m.youtube.com","youtu.be",
 "instagram.com","www.instagram.com",
 "tiktok.com","www.tiktok.com","vm.tiktok.com","vt.tiktok.com"
}
def valid_url(url):
    try:
        p=urlparse(url.strip())
        host=(p.hostname or "").lower()
        return p.scheme=="https" and (host in ALLOWED_HOSTS or any(host.endswith("."+h) for h in ALLOWED_HOSTS))
    except Exception:
        return False

async def ytdlp_download(url, folder):
    import yt_dlp
    opts={"format":"best[height<=720]/best","outtmpl":str(folder/"source.%(ext)s"),
          "noplaylist":True,"max_filesize":MAX_OUTPUT,"socket_timeout":15,
          "retries":1,"fragment_retries":1,"concurrent_fragment_downloads":4,
          "quiet":True,"no_warnings":True,"merge_output_format":"mp4"}
    def work():
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([url])
    await asyncio.to_thread(work)
    fs=[p for p in folder.iterdir() if p.is_file() and p.name.startswith("source.")]
    if not fs:
        raise RuntimeError("Video topilmadi yoki platforma yuklashga ruxsat bermadi.")
    f=max(fs,key=lambda p:p.stat().st_size)
    if f.stat().st_size>MAX_OUTPUT:
        raise RuntimeError("Video 49 MB dan katta.")
    return f

@router.message(F.video | F.video_note)
async def video_without_mode(message: Message):
    row = user_row(message.from_user.id)
    lang = row["lang"] if row and row["lang"] else "uz"
    mode = user_modes.get(message.from_user.id)
    if mode in ("round", "916") and message.video:
        if not await require_access(message, lang):
            return
        await process_uploaded(message, mode, lang)
        return
    await message.answer(tr(lang,"unknown"), reply_markup=main_keyboard(lang,is_pro(message.from_user.id)))

# Link mode text dispatch is implemented as a high-priority middleware-like handler
# by checking link mode at the beginning of the text router via this wrapper.
# (The function below is registered with a filter and runs only for link mode.)
@router.callback_query(F.data.in_({"linkvideo","linkaudio","linkcancel"}))
async def link_choice(call: CallbackQuery):
    uid=call.from_user.id
    mode=user_modes.get(uid,"")
    if not mode.startswith("link_choice:"):
        await call.answer("Havola eskirgan. Qayta yuboring.",show_alert=True); return
    url=mode.split(":",1)[1]
    row=user_row(uid); lang=row["lang"] if row and row["lang"] else "uz"
    if call.data=="linkcancel":
        user_modes.pop(uid,None); await call.message.edit_text("Bekor qilindi."); await call.answer(); return
    audio=call.data=="linkaudio"
    if audio and (not is_pro(uid) or "instagram.com" not in (urlparse(url).hostname or "")):
        await call.answer(tr(lang,"notpro"),show_alert=True); return
    user_modes.pop(uid,None)
    await call.answer()
    status=await call.message.answer("⏳ Video olinmoqda..." if not audio else "⏳ Audio tayyorlanmoqda...")
    try:
        with tempfile.TemporaryDirectory(prefix="asaved_link_") as td:
            folder=Path(td)
            src=await ytdlp_download(url,folder)
            if audio:
                out=folder/"audio.mp3"
                await ffmpeg(["-i",str(src),"-vn","-codec:a","libmp3lame","-b:a","192k",str(out)])
                if out.stat().st_size>MAX_OUTPUT: raise RuntimeError(tr(lang,"too_big"))
                await call.message.answer_audio(FSInputFile(out),caption="🎵 Audio tayyor!")
            else:
                await call.message.answer_video(FSInputFile(src),caption="🎬 Video tayyor!")
        await status.delete()
    except Exception:
        log.exception("Link processing failed")
        await status.edit_text(tr(lang,"fail"))

async def on_startup(app):
    init_db()
    await bot.set_webhook(f"{BASE_URL}/{WEBHOOK_PATH}",secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,allowed_updates=dp.resolve_used_update_types())
    log.info("Webhook set: %s/%s",BASE_URL,WEBHOOK_PATH)

async def on_cleanup(app):
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.session.close()

async def main():
    app=web.Application(client_max_size=2*1024*1024)
    app.router.add_get("/",lambda r:web.Response(text="ASAVED PRO bot is running"))
    app.router.add_get("/health",lambda r:web.Response(text="ok"))
    SimpleRequestHandler(dispatcher=dp,bot=bot,secret_token=WEBHOOK_SECRET).register(app,path=f"/{WEBHOOK_PATH}")
    setup_application(app,dp,bot=bot)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    runner=web.AppRunner(app); await runner.setup()
    site=web.TCPSite(runner,"0.0.0.0",PORT); await site.start()
    log.info("ASAVED started on port %s",PORT)
    await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(main())
