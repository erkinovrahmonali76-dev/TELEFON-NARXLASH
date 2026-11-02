
import os
import re
import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import yt_dlp

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, ReplyKeyboardMarkup,
    KeyboardButton, FSInputFile
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook").strip("/")
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID", "")
PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

MAX_SIZE = 45 * 1024 * 1024
TIMEOUT = 240

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN kiritilmagan")

if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
    raise RuntimeError("WEBHOOK_SECRET kamida 16 belgidan iborat bo‘lsin")

if not BASE_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL topilmadi")

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# Foydalanuvchi qidirgan qo'shiqlar
user_songs = {}
user_video_urls = {}

menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎵 Musiqa qidirish")],
        [KeyboardButton(text="📥 Video yuklash")],
        [KeyboardButton(text="ℹ️ Yordam")]
    ],
    resize_keyboard=True
)


def is_video_url(text):
    try:
        host = (urlparse(text.strip()).hostname or "").lower()
        allowed = (
            "youtube.com", "youtu.be", "youtube-nocookie.com",
            "instagram.com", "instagr.am",
            "tiktok.com", "vm.tiktok.com", "vt.tiktok.com"
        )
        return any(host == d or host.endswith("." + d) for d in allowed)
    except Exception:
        return False


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📥 Saqlash",
                callback_data="save_video"
            ),
            InlineKeyboardButton(
                text="🎵 MP3 olish",
                callback_data="video_mp3"
            )
        ]
    ])


async def jamendo_search(query):
    if not JAMENDO_CLIENT_ID:
        raise RuntimeError("JAMENDO_CLIENT_ID sozlanmagan")

    api_url = "https://api.jamendo.com/v3.0/tracks/"

    params = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        "limit": 10,
        "search": query,
        "audioformat": "mp32",
        "include": "musicinfo",
        "order": "relevance"
    }

    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(api_url, params=params) as response:
            if response.status != 200:
                raise RuntimeError("Musiqa qidiruv xizmati javob bermadi")

            data = await response.json()

    return data.get("results", [])


def song_keyboard():
    rows = []

    for start in range(0, 10, 5):
        row = []
        for index in range(start, min(start + 5, 10)):
            row.append(
                InlineKeyboardButton(
                    text=str(index + 1),
                    callback_data=f"song:{index}"
                )
            )
        rows.append(row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🎧 <b>ASAVED BOT</b>\n\n"
        "🎵 Qo‘shiq nomi yoki ijrochi ismini yozing.\n"
        "🔟 Sizga 10 ta musiqa varianti chiqaraman.\n\n"
        "📥 YouTube, TikTok yoki Instagram havolasini yuboring — "
        "video yuklab beraman.",
        reply_markup=menu
    )


@dp.message(F.text == "ℹ️ Yordam")
async def help_message(message: Message):
    await message.answer(
        "🎵 <b>Musiqa:</b> qo‘shiq nomi yoki ijrochini yozing.\n"
        "📥 <b>Video:</b> YouTube, TikTok yoki Instagram havolasini yuboring.\n"
        "🎧 <b>MP3:</b> video ostidagi MP3 tugmasini bosing.\n\n"
        "Faqat yuklashga ruxsatingiz bo‘lgan kontentdan foydalaning."
    )


@dp.message(F.text == "🎵 Musiqa qidirish")
async def ask_music(message: Message):
    await message.answer(
        "🎵 Qo‘shiq nomi yoki ijrochi ismini yozing:"
    )


@dp.message(F.text == "📥 Video yuklash")
async def ask_video(message: Message):
    await message.answer("📎 Video havolasini yuboring:")


@dp.message(F.text)
async def text_handler(message: Message):
    text = message.text.strip()

    if is_video_url(text):
        await process_video(message, text)
        return

    if text.startswith("/"):
        return

    status = await message.answer("🔎 Musiqalar qidirilmoqda...")

    try:
        results = await jamendo_search(text)

        if not results:
            await status.edit_text("😕 Hech qanday musiqa topilmadi.")
            return

        user_songs[message.from_user.id] = results

        lines = ["🎵 <b>Topilgan musiqalar:</b>\n"]

        for i, song in enumerate(results[:10], 1):
            title = song.get("name", "Noma'lum")
            artist = song.get("artist_name", "Noma'lum")
            duration = int(song.get("duration") or 0)
            minutes, seconds = divmod(duration, 60)

            lines.append(
                f"<b>{i}.</b> {title} — {artist} "
                f"<b>{minutes}:{seconds:02d}</b>"
            )

        lines.append("\n👇 Kerakli raqamni bosing:")

        await status.edit_text(
            "\n".join(lines),
            reply_markup=song_keyboard()
        )

    except Exception as error:
        print("Music search error:", repr(error))
        await status.edit_text(
            "❌ Musiqa qidirishda xatolik.\n"
            "Jamendo API kalitini tekshiring."
        )


async def process_video(message, url):
    status = await message.answer("⏳ Video yuklanmoqda...")

    try:
        with tempfile.TemporaryDirectory() as temp:
            output = str(Path(temp) / "%(id)s.%(ext)s")

            options = {
                "format": (
                    "best[ext=mp4][vcodec!=none][acodec!=none]"
                    "/best[ext=mp4]/best"
                ),
                "outtmpl": output,
                "merge_output_format": "mp4",
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 30,
                "retries": 2,
                "max_filesize": MAX_SIZE,
            }

            await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: download_youtube(url, options)
                ),
                timeout=TIMEOUT
            )

            files = [
                p for p in Path(temp).iterdir()
                if p.is_file() and p.suffix.lower() not in (".part",)
            ]

            if not files:
                raise RuntimeError("Video fayli topilmadi")

            filepath = max(files, key=lambda p: p.stat().st_size)

            if filepath.stat().st_size > MAX_SIZE:
                raise RuntimeError("Video 45 MB dan katta")

            user_video_urls[message.from_user.id] = url

            await status.delete()

            await message.answer_video(
                FSInputFile(filepath),
                caption="📥 <b>ASAVED BOT</b>",
                reply_markup=main_keyboard(),
                supports_streaming=True
            )

    except asyncio.TimeoutError:
        await status.edit_text("⌛ Yuklash vaqti tugadi.")
    except Exception as error:
        print("Video error:", repr(error))
        await status.edit_text(
            "❌ Video yuklanmadi.\n"
            "Havolani tekshiring yoki keyinroq urinib ko‘ring."
        )


def download_youtube(url, options):
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])


async def download_audio(url, folder):
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(Path(folder) / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_SIZE,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192"
        }]
    }

    await asyncio.wait_for(
        asyncio.to_thread(
            lambda: download_youtube(url, options)
        ),
        timeout=TIMEOUT
    )

    files = list(Path(folder).glob("*.mp3"))

    if not files:
        raise RuntimeError("MP3 fayli topilmadi")

    return files[0]


async def download_jamendo_song(song):
    url = song.get("audiodownload")

    if not url:
        raise RuntimeError(
            "Bu qo‘shiq uchun ruxsat etilgan MP3 yuklash havolasi yo‘q"
        )

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "music.mp3"
        timeout = aiohttp.ClientTimeout(total=120)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise RuntimeError("MP3 yuklab bo‘lmadi")

                total = 0
                with open(path, "wb") as file:
                    async for chunk in response.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > MAX_SIZE:
                            raise RuntimeError("MP3 fayli juda katta")
                        file.write(chunk)

        if path.stat().st_size == 0:
            raise RuntimeError("Bo‘sh MP3 fayli")

        # Fayl yuborilguncha vaqtinchalik papka o‘chmasligi kerak.
        yield path


@dp.callback_query(F.data.startswith("song:"))
async def select_song(callback: CallbackQuery):
    try:
        index = int(callback.data.split(":")[1])
        songs = user_songs.get(callback.from_user.id, [])

        if index < 0 or index >= len(songs):
            await callback.answer("Qidiruvni qayta bajaring.", show_alert=True)
            return

        song = songs[index]
        await callback.answer("MP3 tayyorlanmoqda...")

        async for path in download_jamendo_song(song):
            await callback.message.answer_audio(
                FSInputFile(path),
                title=song.get("name", "Music"),
                performer=song.get("artist_name", "Unknown"),
                caption="🎵 <b>ASAVED BOT</b>"
            )

    except Exception as error:
        print("Song download error:", repr(error))
        await callback.message.answer(
            "❌ Bu musiqani MP3 qilib yuklab bo‘lmadi. "
            "Boshqa qo‘shiqni tanlang."
        )


@dp.callback_query(F.data == "save_video")
async def save_video(callback: CallbackQuery):
    await callback.answer(
        "Videoni Telegramdagi yuklab olish tugmasi orqali saqlang.",
        show_alert=True
    )


@dp.callback_query(F.data == "video_mp3")
async def video_mp3(callback: CallbackQuery):
    url = user_video_urls.get(callback.from_user.id)

    if not url:
        await callback.answer("Avval video yuboring.", show_alert=True)
        return

    await callback.answer("MP3 tayyorlanmoqda...")

    status = await callback.message.answer("🎧 Audio ajratilmoqda...")

    try:
        with tempfile.TemporaryDirectory() as folder:
            path = await download_audio(url, folder)

            await callback.message.answer_audio(
                FSInputFile(path),
                caption="🎵 <b>ASAVED BOT</b>"
            )

        await status.delete()

    except Exception as error:
        print("Audio extraction error:", repr(error))
        await status.edit_text(
            "❌ MP3 tayyorlanmadi. Keyinroq urinib ko‘ring."
        )


async def health(request):
    return web.json_response({"status": "ok", "bot": "ASAVED"})


async def telegram_webhook(request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise web.HTTPUnauthorized()

    try:
        update = await request.json()
        from aiogram.types import Update
        telegram_update = Update.model_validate(
            update, context={"bot": bot}
        )
        await dp.feed_update(bot, telegram_update)
    except Exception as error:
        print("Webhook error:", repr(error))

    return web.Response(text="OK")


async def on_startup(app):
    await bot.set_webhook(
        url=f"{BASE_URL}/{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True
    )
    print("ASAVED webhook started")


async def on_shutdown(app):
    await bot.delete_webhook()
    await bot.session.close()


app = web.Application(client_max_size=2 * 1024 * 1024)
app.router.add_get("/", health)
app.router.add_get("/health", health)
app.router.add_post(f"/{WEBHOOK_PATH}", telegram_webhook)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_shutdown)


if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
