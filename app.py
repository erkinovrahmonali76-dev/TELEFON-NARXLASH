import os
import re
import html
import glob
import asyncio
import logging
import tempfile
import subprocess
import aiohttp
import yt_dlp
from urllib.parse import urlparse
from datetime import timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
# =====================================
# SETTINGS
# =====================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", "10000"))
MAX_FILE_SIZE = 49 * 1024 * 1024
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ASAVED")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN kiritilmagan")
if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
    raise RuntimeError("WEBHOOK_SECRET kamida 16 belgi bo‘lsin")
if not RENDER_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL topilmadi")
bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
search_cache = {}
pending_videos = {}
# =====================================
# YOUTUBE DURATION
# =====================================
def format_duration(value):
    match = re.fullmatch(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
        value or ""
    )
    if not match:
        return "Noma'lum"
    hours, minutes, seconds = (
        int(x or 0) for x in match.groups()
    )
    total = hours * 3600 + minutes * 60 + seconds
    return str(timedelta(seconds=total))
# =====================================
# YOUTUBE SEARCH
# =====================================
async def youtube_search(query, page_token=None):
    if not YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY kiritilmagan")
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "key": YOUTUBE_API_KEY,
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": 10,
        "order": "relevance",
        "regionCode": "UZ",
        "pageToken": page_token
    }
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as response:
            data = await response.json()
            if response.status != 200:
                logger.error("YOUTUBE ERROR: %s", data)
                raise RuntimeError("YouTube qidiruv xatosi")
        items = data.get("items", [])
        video_ids = [
            item["id"]["videoId"]
            for item in items
            if item.get("id", {}).get("videoId")
        ]
        durations = {}
        if video_ids:
            details_url = (
                "https://www.googleapis.com/youtube/v3/videos"
            )
            details_params = {
                "key": YOUTUBE_API_KEY,
                "part": "contentDetails",
                "id": ",".join(video_ids)
            }
            async with session.get(
                details_url,
                params=details_params
            ) as response:
                details = await response.json()
                if response.status == 200:
                    for item in details.get("items", []):
                        durations[item["id"]] = format_duration(
                            item["contentDetails"]["duration"]
                        )
        for item in items:
            video_id = item["id"]["videoId"]
            item["duration"] = durations.get(
                video_id,
                "Noma'lum"
            )
        return {
            "items": items,
            "nextPageToken": data.get("nextPageToken"),
            "prevPageToken": data.get("prevPageToken")
        }
# =====================================
# KEYBOARD
# =====================================
def results_keyboard():
    rows = []
    rows.append([
        InlineKeyboardButton(
            text=str(i),
            callback_data=f"track:{i-1}"
        )
        for i in range(5)
    ])
    rows.append([
        InlineKeyboardButton(
            text=str(i),
            callback_data=f"track:{i-1}"
        )
        for i in range(5, 10)
    ])
    rows.append([
        InlineKeyboardButton(
            text="⬅️",
            callback_data="music_prev"
        ),
        InlineKeyboardButton(
            text="❌",
            callback_data="music_close"
        ),
        InlineKeyboardButton(
            text="➡️",
            callback_data="music_next"
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
# =====================================
# RESULTS TEXT
# =====================================
def make_results_text(query, items, page):
    lines = [
        f"🎵 <b>{html.escape(query)}</b>",
        f"<i>YouTube musiqa qidiruvi | Sahifa {page + 1}</i>",
        ""
    ]
    for index, item in enumerate(items, 1):
        snippet = item["snippet"]
        title = html.escape(snippet["title"])
        artist = html.escape(snippet["channelTitle"])
        duration = item.get("duration", "Noma'lum")
        lines.append(
            f"<b>{index}.</b> {title}\n"
            f"👤 {artist} | ⏱ {duration}\n"
        )
    lines.append("👇 Qo‘shiq raqamini tanlang.")
    return "\n".join(lines)
# =====================================
# SEARCH RESULTS
# =====================================
async def show_music(message, query):
    loading = await message.answer(
        "🔍 YouTube'dan musiqa qidirilmoqda..."
    )
    try:
        data = await youtube_search(query)
        items = data["items"]
        if not items:
            await loading.edit_text(
                "😕 Hech qanday natija topilmadi."
            )
            return
        chat_id = message.chat.id
        search_cache[chat_id] = {
            "query": query,
            "items": items,
            "page": 0,
            "next": data["nextPageToken"],
            "tokens": [None]
        }
        await loading.edit_text(
            make_results_text(query, items, 0),
            reply_markup=results_keyboard()
        )
    except Exception:
        logger.exception("SEARCH ERROR")
        await loading.edit_text(
            "⚠️ YouTube qidiruvida xatolik.\n"
            "API kalitini tekshiring."
        )
# =====================================
# NEXT PAGE
# =====================================
@dp.callback_query(F.data == "music_next")
async def music_next(call):
    data = search_cache.get(call.message.chat.id)
    if not data:
        await call.answer("Qidiruv tugagan.", show_alert=True)
        return
    token = data["next"]
    if not token:
        await call.answer(
            "Boshqa natija yo‘q.",
            show_alert=True
        )
        return
    await call.answer("Keyingi sahifa...")
    try:
        result = await youtube_search(
            data["query"],
            token
        )
        items = result["items"]
        if not items:
            await call.answer("Natija yo‘q.")
            return
        data["page"] += 1
        data["items"] = items
        data["next"] = result["nextPageToken"]
        data["tokens"] = data["tokens"][:data["page"]]
        data["tokens"].append(token)
        await call.message.edit_text(
            make_results_text(
                data["query"],
                items,
                data["page"]
            ),
            reply_markup=results_keyboard()
        )
    except Exception:
        logger.exception("NEXT PAGE ERROR")
        await call.message.answer("⚠️ Keyingi sahifa ochilmadi.")
# =====================================
# PREVIOUS PAGE
# =====================================
@dp.callback_query(F.data == "music_prev")
async def music_prev(call):
    data = search_cache.get(call.message.chat.id)
    if not data:
        await call.answer("Qidiruv tugagan.", show_alert=True)
        return
    if data["page"] <= 0:
        await call.answer("Birinchi sahifadasiz.")
        return
    await call.answer("Oldingi sahifa...")
    try:
        token = data["tokens"][data["page"] - 1]
        result = await youtube_search(
            data["query"],
            token
        )
        data["page"] -= 1
        data["items"] = result["items"]
        data["next"] = result["nextPageToken"]
        await call.message.edit_text(
            make_results_text(
                data["query"],
                data["items"],
                data["page"]
            ),
            reply_markup=results_keyboard()
        )
    except Exception:
        logger.exception("PREVIOUS PAGE ERROR")
        await call.message.answer("⚠️ Sahifa ochilmadi.")
# =====================================
# SELECT MUSIC
# =====================================
@dp.callback_query(F.data.startswith("track:"))
async def select_track(call):
    try:
        index = int(call.data.split(":")[1])
        data = search_cache.get(call.message.chat.id)
        if not data:
            await call.answer(
                "Qidiruv muddati tugadi.",
                show_alert=True
            )
            return
        items = data["items"]
        if index < 0 or index >= len(items):
            await call.answer("Noto‘g‘ri raqam.")
            return
        item = items[index]
        video_id = item["id"]["videoId"]
        snippet = item["snippet"]
        title = html.escape(snippet["title"])
        artist = html.escape(snippet["channelTitle"])
        youtube_url = (
            f"https://www.youtube.com/watch?v={video_id}"
        )
        thumbnail = (
            snippet.get("thumbnails", {})
            .get("high", {})
            .get("url")
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="▶️ YouTube'da tinglash",
                        url=youtube_url
                    )
                ]
            ]
        )
        caption = (
            f"🎵 <b>{title}</b>\n\n"
            f"👤 {artist}\n"
            f"⏱ {item.get('duration', 'Noma\\'lum')}\n\n"
            "🎧 <b>ASAVED MUSIC</b>"
        )
        await call.answer()
        if thumbnail:
            try:
                await call.message.answer_photo(
                    photo=thumbnail,
                    caption=caption,
                    reply_markup=keyboard
                )
                return
            except Exception:
                logger.exception("Thumbnail yuborilmadi")
        await call.message.answer(
            caption + f"\n\n{youtube_url}",
            reply_markup=keyboard
        )
    except Exception:
        logger.exception("SELECT TRACK ERROR")
        await call.answer(
            "Xatolik yuz berdi.",
            show_alert=True
        )
# =====================================
# CLOSE SEARCH
# =====================================
@dp.callback_query(F.data == "music_close")
async def music_close(call):
    search_cache.pop(call.message.chat.id, None)
    await call.message.edit_text(
        "❌ Musiqa qidiruvi yopildi."
    )
    await call.answer()
# =====================================
# VIDEO URLS
# =====================================
ALLOWED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com"
)
def valid_video_url(url):
    try:
        host = (urlparse(url).hostname or "").lower()
        return any(
            host == domain or host.endswith("." + domain)
            for domain in ALLOWED_DOMAINS
        )
    except Exception:
        return False
async def ask_video_permission(message, url):
    key = f"{message.chat.id}:{message.from_user.id}"
    pending_videos[key] = url
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Ruxsatim bor",
                    callback_data="video_confirm"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Bekor qilish",
                    callback_data="video_cancel"
                )
            ]
        ]
    )
    await message.answer(
        "🎬 Video havolasi qabul qilindi.\n\n"
        "Faqat o‘zingizga tegishli yoki yuklashga "
        "ruxsatingiz bor videoni davom ettiring.",
        reply_markup=keyboard
    )
# =====================================
# VIDEO DOWNLOAD
# =====================================
def download_video(url, folder):
    output = os.path.join(
        folder,
        "source.%(ext)s"
    )
    options = {
        "outtmpl": output,
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "max_filesize": 150 * 1024 * 1024
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])
    files = glob.glob(
        os.path.join(folder, "source.*")
    )
    if not files:
        raise RuntimeError("Video topilmadi")
    return max(files, key=os.path.getsize)
# =====================================
# CONVERT TO 9:16
# =====================================
def convert_vertical(source, destination):
    command = [
        "ffmpeg",
        "-y",
        "-i", source,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-vf",
        (
            "scale=1080:1920:"
            "force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
            "setsar=1"
        ),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "27",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-t", "600",
        "-movflags", "+faststart",
        destination
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=900
    )
    if result.returncode != 0:
        logger.error(result.stderr[-2000:])
        raise RuntimeError("Video konvertatsiya xatosi")
async def process_video(message, url):
    status = await message.answer(
        "⏳ Video yuklanmoqda..."
    )
    try:
        with tempfile.TemporaryDirectory() as folder:
            source = await asyncio.to_thread(
                download_video,
                url,
                folder
            )
            await status.edit_text(
                "📐 Video 9:16 formatga o‘tkazilmoqda..."
            )
            destination = os.path.join(
                folder,
                "ASAVED_9x16.mp4"
            )
            await asyncio.to_thread(
                convert_vertical,
                source,
                destination
            )
            if os.path.getsize(destination) > MAX_FILE_SIZE:
                await status.edit_text(
                    "⚠️ Video Telegram yuborish limitidan katta."
                )
                return
            await status.edit_text(
                "📤 Telegramga yuborilmoqda..."
            )
            await message.answer_video(
                video=FSInputFile(destination),
                caption=(
                    "🎬 <b>ASAVED VIDEO</b>\n"
                    "📐 9:16 | 1080×1920"
                ),
                supports_streaming=True
            )
            await status.delete()
    except Exception:
        logger.exception("VIDEO ERROR")
        await status.edit_text(
            "⚠️ Video yuklashda xatolik yuz berdi.\n"
            "Boshqa havola bilan urinib ko‘ring."
        )
@dp.callback_query(F.data == "video_confirm")
async def video_confirm(call):
    key = f"{call.message.chat.id}:{call.from_user.id}"
    url = pending_videos.pop(key, None)
    if not url:
        await call.answer(
            "Havola topilmadi. Qayta yuboring.",
            show_alert=True
        )
        return
    await call.answer("Boshlanmoqda...")
    await process_video(
        call.message,
        url
    )
@dp.callback_query(F.data == "video_cancel")
async def video_cancel(call):
    key = f"{call.message.chat.id}:{call.from_user.id}"
    pending_videos.pop(key, None)
    await call.message.edit_text(
        "❌ Video bekor qilindi."
    )
    await call.answer()
# =====================================
# START
# =====================================
@dp.message(CommandStart())
async def start(message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎵 Musiqa qidirish",
                    callback_data="music_help"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎬 Video 9:16",
                    callback_data="video_help"
                )
            ]
        ]
    )
    await message.answer(
        "🎧 <b>ASAVED MUSIC BOT</b>\n\n"
        "🎵 YouTube'dan musiqa qidiring.\n"
        "🎬 Videoni 9:16 formatga aylantiring.\n\n"
        "Musiqa nomini yozing yoki video havolasini yuboring.",
        reply_markup=keyboard
    )
@dp.callback_query(F.data == "music_help")
async def music_help(call):
    await call.message.answer(
        "🎵 Qo‘shiq yoki ijrochi nomini yuboring.\n"
        "Masalan: Macan"
    )
    await call.answer()
@dp.callback_query(F.data == "video_help")
async def video_help(call):
    await call.message.answer(
        "🎬 Video havolasini yuboring."
    )
    await call.answer()
@dp.message(Command("help"))
async def help_command(message):
    await message.answer(
        "🎵 Musiqa qidirish: nomini yozing.\n"
        "🎬 Video: havolani yuboring.\n"
        "/start — Bosh menyu"
    )
# =====================================
# TEXT HANDLER
# =====================================
@dp.message(F.text)
async def text_handler(message):
    text = message.text.strip()
    if text.startswith("/"):
        return
    if valid_video_url(text):
        await ask_video_permission(message, text)
        return
    await show_music(message, text)
# =====================================
# WEBHOOK
# =====================================
async def health(request):
    return web.json_response({
        "status": "ok",
        "bot": "ASAVED"
    })
async def telegram_webhook(request):
    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )
    if secret != WEBHOOK_SECRET:
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
        logger.exception("WEBHOOK ERROR")
        return web.Response(status=500)
# =====================================
# MAIN
# =====================================
async def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post(
        f"/{WEBHOOK_PATH}",
        telegram_webhook
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )
    await site.start()
    webhook_url = (
        f"{RENDER_URL.rstrip('/')}/{WEBHOOK_PATH}"
    )
    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True
    )
    logger.info("ASAVED ISHGA TUSHDI")
    logger.info("WEBHOOK: %s", webhook_url)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await bot.session.close()
if __name__ == "__main__":
    asyncio.run(main())
