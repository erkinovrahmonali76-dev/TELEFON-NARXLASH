
import asyncio
import logging
import os
import tempfile
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    KeyboardButton,
    ReplyKeyboardMarkup,
    FSInputFile,
    Update,
)

from video import convert_video
from instagram import valid_instagram_url, get_stories

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "asaved_secret_2026")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook")
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

MAX_SIZE = 45 * 1024 * 1024

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ASAVED")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN kiritilmagan")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

modes = {}

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="⭕ Dumaloq video"),
            KeyboardButton(text="📱 9:16 format"),
        ],
        [
            KeyboardButton(text="📸 Instagram Stories"),
        ],
    ],
    resize_keyboard=True
)


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👋 ASAVED botiga xush kelibsiz!\n\n"
        "⭕ Dumaloq video\n"
        "📱 9:16 format\n"
        "📸 Instagram Stories",
        reply_markup=keyboard
    )


@dp.message(F.text == "⭕ Dumaloq video")
async def round_mode(message: Message):
    modes[message.from_user.id] = "round"
    await message.answer("Dumaloq qilmoqchi bo'lgan videoni yuboring.")


@dp.message(F.text == "📱 9:16 format")
async def vertical_mode(message: Message):
    modes[message.from_user.id] = "916"
    await message.answer("9:16 qilmoqchi bo'lgan videoni yuboring.")


@dp.message(F.text == "📸 Instagram Stories")
async def stories_mode(message: Message):
    modes[message.from_user.id] = "stories"
    await message.answer("Ochiq Instagram akkaunti havolasini yuboring.")


@dp.message(F.video)
async def video_handler(message: Message):
    uid = message.from_user.id
    mode = modes.get(uid)

    if mode not in ("round", "916"):
        await message.answer("Avval video formatini tanlang.")
        return

    if message.video.file_size and message.video.file_size > MAX_SIZE:
        await message.answer("Video hajmi juda katta.")
        return

    status = await message.answer("⏳ Video tayyorlanmoqda...")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            output = Path(tmp) / "output.mp4"

            file = await bot.get_file(message.video.file_id)
            await bot.download_file(file.file_path, destination=source)

            await convert_video(source, output, mode)

            if mode == "round":
                await message.answer_video_note(
                    video=FSInputFile(output)
                )
            else:
                await message.answer_video(
                    video=FSInputFile(output),
                    caption="✅ 9:16 video tayyor!"
                )

        await status.delete()

    except Exception as e:
        log.exception("VIDEO ERROR")
        await status.edit_text(f"❌ Xatolik: {str(e)[:500]}")

    finally:
        modes.pop(uid, None)


@dp.message(F.text)
async def text_handler(message: Message):
    url = (message.text or "").strip()

    if valid_instagram_url(url):
        if modes.get(message.from_user.id) != "stories":
            await message.answer(
                "Avval 📸 Instagram Stories tugmasini bosing."
            )
            return

        username = url.rstrip("/").split("/")[-1]

        try:
            stories = await get_stories(username)
            await message.answer(str(stories))
        except Exception as e:
            await message.answer(f"❌ {e}")

        return

    if url.startswith(("https://", "http://")):
        await message.answer("Bu havola qo'llab-quvvatlanmaydi.")


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
