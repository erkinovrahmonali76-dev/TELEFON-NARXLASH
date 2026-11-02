
import logging
import os
import tempfile
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile,
    Update,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from video import convert_video

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "asaved_secret_2026")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook")
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

# KANAL SOZLAMALARI
CHANNEL_ID = "@ReklamaBozoriUzz"
CHANNEL_URL = "https://t.me/ReklamaBozoriUzz"

MAX_SIZE = 45 * 1024 * 1024

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ASAVED")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN kiritilmagan")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

users = {}
modes = {}

TEXTS = {
    "uz": {
        "welcome": "👋 ASAVED botiga xush kelibsiz!",
        "choose": "Kerakli xizmatni tanlang:",
        "round": "⭕ Dumaloq video",
        "vertical": "📱 9:16 format",
        "send_round": "Dumaloq qilmoqchi bo'lgan videoni yuboring.",
        "send_vertical": "9:16 qilmoqchi bo'lgan videoni yuboring.",
        "processing": "⏳ Video tayyorlanmoqda...",
        "done": "✅ Video tayyor!",
        "error": "❌ Video bilan ishlashda xatolik yuz berdi.",
        "subscribe": "📢 Botdan foydalanish uchun avval kanalimizga obuna bo'ling.",
        "check": "✅ Obunani tekshirish",
        "not_sub": "❌ Siz hali kanalga obuna bo'lmagansiz.",
        "language": "🌐 Tilni tanlang:",
        "size": "❌ Video hajmi 45 MB dan oshmasligi kerak.",
        "first": "Avval xizmatni tanlang."
    },
    "ru": {
        "welcome": "👋 Добро пожаловать в ASAVED!",
        "choose": "Выберите услугу:",
        "round": "⭕ Круглое видео",
        "vertical": "📱 Формат 9:16",
        "send_round": "Отправьте видео для круглого формата.",
        "send_vertical": "Отправьте видео для формата 9:16.",
        "processing": "⏳ Обрабатываем видео...",
        "done": "✅ Видео готово!",
        "error": "❌ Ошибка обработки видео.",
        "subscribe": "📢 Сначала подпишитесь на наш канал.",
        "check": "✅ Проверить подписку",
        "not_sub": "❌ Вы ещё не подписались.",
        "language": "🌐 Выберите язык:",
        "size": "❌ Видео не должно превышать 45 МБ.",
        "first": "Сначала выберите услугу."
    },
    "en": {
        "welcome": "👋 Welcome to ASAVED bot!",
        "choose": "Choose a service:",
        "round": "⭕ Round video",
        "vertical": "📱 9:16 format",
        "send_round": "Send a video to make it round.",
        "send_vertical": "Send a video for 9:16 format.",
        "processing": "⏳ Processing video...",
        "done": "✅ Video is ready!",
        "error": "❌ Video processing failed.",
        "subscribe": "📢 Subscribe to our channel before using the bot.",
        "check": "✅ Check subscription",
        "not_sub": "❌ You haven't subscribed yet.",
        "language": "🌐 Choose your language:",
        "size": "❌ Video must not exceed 45 MB.",
        "first": "Choose a service first."
    }
}


def lang_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🇺🇿 O'zbekcha", callback_data="lang_uz")
    builder.button(text="🇷🇺 Русский", callback_data="lang_ru")
    builder.button(text="🇬🇧 English", callback_data="lang_en")
    builder.adjust(1)
    return builder.as_markup()


def main_keyboard(lang):
    t = TEXTS[lang]
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=t["round"]),
                KeyboardButton(text=t["vertical"])
            ]
        ],
        resize_keyboard=True
    )


def subscription_keyboard(lang):
    t = TEXTS[lang]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 Kanalga obuna bo'lish",
                    url=CHANNEL_URL
                )
            ],
            [
                InlineKeyboardButton(
                    text=t["check"],
                    callback_data="check_sub"
                )
            ]
        ]
    )


async def is_subscribed(user_id):
    try:
        member = await bot.get_chat_member(
            chat_id=CHANNEL_ID,
            user_id=user_id
        )
        return member.status in (
            "member", "administrator", "creator"
        )
    except Exception:
        log.exception("SUBSCRIPTION CHECK ERROR")
        return False


async def show_menu(message, lang):
    t = TEXTS[lang]
    await message.answer(
        t["welcome"] + "\n\n" + t["choose"],
        reply_markup=main_keyboard(lang)
    )


@dp.message(CommandStart())
async def start(message: Message):
    uid = message.from_user.id
    lang = users.get(uid, "uz")

    if not await is_subscribed(uid):
        await message.answer(
            TEXTS[lang]["subscribe"],
            reply_markup=subscription_keyboard(lang)
        )
        return

    if uid not in users:
        await message.answer(
            "🌐 Tilni tanlang / Выберите язык / Choose language:",
            reply_markup=lang_keyboard()
        )
        return

    await show_menu(message, lang)


@dp.callback_query(F.data.startswith("lang_"))
async def language_choice(call: CallbackQuery):
    lang = call.data.replace("lang_", "")

    if lang not in TEXTS:
        await call.answer()
        return

    users[call.from_user.id] = lang
    await call.answer()

    if not await is_subscribed(call.from_user.id):
        await call.message.answer(
            TEXTS[lang]["subscribe"],
            reply_markup=subscription_keyboard(lang)
        )
        return

    await show_menu(call.message, lang)


@dp.callback_query(F.data == "check_sub")
async def check_subscription(call: CallbackQuery):
    uid = call.from_user.id
    lang = users.get(uid, "uz")

    if not await is_subscribed(uid):
        await call.answer(
            TEXTS[lang]["not_sub"],
            show_alert=True
        )
        return

    await call.answer("✅ Obuna tasdiqlandi!")

    if uid not in users:
        await call.message.answer(
            TEXTS[lang]["language"],
            reply_markup=lang_keyboard()
        )
    else:
        await show_menu(call.message, lang)


@dp.message(F.text)
async def choose_mode(message: Message):
    uid = message.from_user.id
    lang = users.get(uid, "uz")
    t = TEXTS[lang]

    if not await is_subscribed(uid):
        await message.answer(
            t["subscribe"],
            reply_markup=subscription_keyboard(lang)
        )
        return

    if message.text == t["round"]:
        modes[uid] = "round"
        await message.answer(t["send_round"])

    elif message.text == t["vertical"]:
        modes[uid] = "916"
        await message.answer(t["send_vertical"])


@dp.message(F.video)
async def video_handler(message: Message):
    uid = message.from_user.id
    lang = users.get(uid, "uz")
    t = TEXTS[lang]

    if not await is_subscribed(uid):
        await message.answer(
            t["subscribe"],
            reply_markup=subscription_keyboard(lang)
        )
        return

    mode = modes.get(uid)

    if mode not in ("round", "916"):
        await message.answer(t["first"])
        return

    if message.video.file_size and message.video.file_size > MAX_SIZE:
        await message.answer(t["size"])
        return

    status = await message.answer(t["processing"])

    try:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            output = Path(tmp) / "output.mp4"

            file = await bot.get_file(message.video.file_id)
            await bot.download_file(
                file.file_path,
                destination=source
            )

            await convert_video(source, output, mode)

            if mode == "round":
                await message.answer_video_note(
                    video=FSInputFile(output)
                )
            else:
                await message.answer_video(
                    video=FSInputFile(output),
                    caption=t["done"]
                )

        await status.delete()

    except Exception:
        log.exception("VIDEO ERROR")
        await status.edit_text(t["error"])

    finally:
        modes.pop(uid, None)


async def health(request):
    return web.Response(text="ASAVED OK")


async def webhook(request):
    if request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    ) != WEBHOOK_SECRET:
        return web.Response(status=403)

    try:
        data = await request.json()
        update = Update.model_validate(
            data,
            context={"bot": bot}
        )
        await dp.feed_update(bot, update)
        return web.Response(text="OK")
    except Exception:
        log.exception("WEBHOOK ERROR")
        return web.Response(status=500)


async def startup(app):
    if not BASE_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL topilmadi")

    await bot.set_webhook(
        f"{BASE_URL}/{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET
    )


async def shutdown(app):
    await bot.session.close()


app = web.Application()
app.router.add_get("/", health)
app.router.add_post(f"/{WEBHOOK_PATH}", webhook)
app.on_startup.append(startup)
app.on_shutdown.append(shutdown)


if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
