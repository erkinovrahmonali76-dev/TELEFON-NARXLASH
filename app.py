import os
import re
import logging
from datetime import timedelta
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.webhook.aiohttp_server import (
    SimpleRequestHandler,
    setup_application,
)
# =========================
# SOZLAMALAR
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
WEBHOOK_PATH = os.getenv(
    "WEBHOOK_PATH", "asaved-webhook"
).strip("/")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", "10000"))
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ASAVED")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN kiritilmagan")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET kiritilmagan")
if not RENDER_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL topilmadi")
if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET noto'g'ri formatda")
# =========================
# BOT
# =========================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
user_modes = {}
youtube_results = {}
youtube_tokens = {}
# =========================
# MENYU
# =========================
def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎵 YouTube qidirish",
                    callback_data="mode_youtube"
                )
            ]
        ]
    )
def back_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏠 Bosh menyu",
                    callback_data="home"
                )
            ]
        ]
    )
# =========================
# DAVOMIYLIK
# =========================
def format_duration(value):
    if not value:
        return "Noma'lum"
    match = re.fullmatch(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
        value
    )
    if not match:
        return "Noma'lum"
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return str(timedelta(
        hours=hours,
        minutes=minutes,
        seconds=seconds
    ))
# =========================
# YOUTUBE QIDIRUV
# =========================
async def youtube_search(query, page_token=None):
    if not YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY kiritilmagan")
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": 10,
            "key": YOUTUBE_API_KEY,
        }
        if page_token:
            params["pageToken"] = page_token
        url = "https://www.googleapis.com/youtube/v3/search"
        async with session.get(url, params=params) as response:
            data = await response.json()
            if response.status != 200:
                logger.error("YouTube API xatosi: %s", data)
                error = data.get("error", {}).get(
                    "message", "YouTube API xatosi"
                )
                raise RuntimeError(error)
        items = data.get("items", [])
        video_ids = [
            item["id"]["videoId"]
            for item in items
            if item.get("id", {}).get("videoId")
        ]
        durations = {}
        if video_ids:
            duration_params = {
                "part": "contentDetails",
                "id": ",".join(video_ids),
                "key": YOUTUBE_API_KEY,
            }
            async with session.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params=duration_params
            ) as response:
                duration_data = await response.json()
                if response.status == 200:
                    for video in duration_data.get("items", []):
                        durations[video["id"]] = format_duration(
                            video.get("contentDetails", {}).get("duration")
                        )
        results = []
        for item in items:
            video_id = item["id"]["videoId"]
            snippet = item.get("snippet", {})
            thumbnails = snippet.get("thumbnails", {})
            thumbnail = (
                thumbnails.get("high", {}).get("url")
                or thumbnails.get("medium", {}).get("url")
                or thumbnails.get("default", {}).get("url")
            )
            results.append({
                "id": video_id,
                "title": snippet.get("title", "Noma'lum"),
                "artist": snippet.get("channelTitle", "Noma'lum"),
                "duration": durations.get(video_id, "Noma'lum"),
                "thumbnail": thumbnail,
                "url": f"https://www.youtube.com/watch?v={video_id}",
            })
        return (
            results,
            data.get("nextPageToken"),
            data.get("prevPageToken")
        )
# =========================
# NATIJALARNI CHIQARISH
# =========================
async def show_youtube_results(message, user_id, page=0):
    results = youtube_results.get(user_id, [])
    if not results:
        await message.answer(
            "Natija topilmadi.",
            reply_markup=back_keyboard()
        )
        return
    buttons = []
    for index, item in enumerate(results):
        buttons.append([
            InlineKeyboardButton(
                text=f"{index + 1}. {item['title'][:45]}",
                callback_data=f"yt:{index}"
            )
        ])
    navigation = []
    tokens = youtube_tokens.get(user_id, {})
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️ Oldingi",
                callback_data=f"ytp:{page - 1}"
            )
        )
    if tokens.get(page + 1):
        navigation.append(
            InlineKeyboardButton(
                text="Keyingi ➡️",
                callback_data=f"ytp:{page + 1}"
            )
        )
    if navigation:
        buttons.append(navigation)
    buttons.append([
        InlineKeyboardButton(
            text="🏠 Bosh menyu",
            callback_data="home"
        )
    ])
    text = "🎵 <b>YOUTUBE NATIJALARI</b>\n\n"
    for index, item in enumerate(results, start=1):
        text += (
            f"<b>{index}.</b> {item['title']}\n"
            f"👤 {item['artist']}\n"
            f"⏱ {item['duration']}\n\n"
        )
    text += "Kerakli qo'shiqni tanlang:"
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        )
    )
# =========================
# START
# =========================
@dp.message(CommandStart())
async def start(message: Message):
    user_modes.pop(message.from_user.id, None)
    await message.answer(
        "🎵 <b>ASAVED MUSIC BOT</b>\n\n"
        "YouTube'dan qo'shiq yoki ijrochi nomini "
        "qidirish uchun tugmani bosing.",
        reply_markup=main_keyboard()
    )
# =========================
# BOSH MENYU
# =========================
@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery):
    user_modes.pop(call.from_user.id, None)
    await call.answer()
    await call.message.answer(
        "🏠 <b>ASAVED MUSIC</b>\n\nXizmatni tanlang:",
        reply_markup=main_keyboard()
    )
# =========================
# YOUTUBE MENYU
# =========================
@dp.callback_query(F.data == "mode_youtube")
async def mode_youtube(call: CallbackQuery):
    user_modes[call.from_user.id] = "youtube"
    await call.answer()
    await call.message.answer(
        "🔎 YouTube'dan qidirmoqchi bo'lgan "
        "qo'shiq yoki ijrochi nomini yuboring:",
        reply_markup=back_keyboard()
    )
# =========================
# QIDIRUV
# =========================
@dp.message(F.text)
async def search_message(message: Message):
    user_id = message.from_user.id
    if user_modes.get(user_id) != "youtube":
        await message.answer(
            "Avval YouTube qidirish tugmasini bosing.",
            reply_markup=main_keyboard()
        )
        return
    query = message.text.strip()
    if len(query) < 2:
        await message.answer("Kamida 2 ta belgi yozing.")
        return
    status = await message.answer(
        "🔎 YouTube'dan qidirilmoqda..."
    )
    try:
        results, next_token, _ = await youtube_search(query)
        youtube_results[user_id] = results
        youtube_tokens[user_id] = {0: None}
        if next_token:
            youtube_tokens[user_id][1] = next_token
        await status.delete()
        if not results:
            await message.answer(
                "YouTube'da natija topilmadi.",
                reply_markup=back_keyboard()
            )
            return
        await show_youtube_results(message, user_id, 0)
    except Exception as error:
        logger.exception("YouTube qidiruv xatosi")
        await status.edit_text(
            f"❌ Qidiruvda xatolik:\n{str(error)[:700]}"
        )
# =========================
# KEYINGI VA OLDINGI SAHIFA
# =========================
@dp.callback_query(F.data.startswith("ytp:"))
async def youtube_page(call: CallbackQuery):
    user_id = call.from_user.id
    try:
        page = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Sahifa noto'g'ri.", show_alert=True)
        return
    tokens = youtube_tokens.get(user_id, {})
    if page not in tokens:
        await call.answer(
            "Bu sahifa mavjud emas.",
            show_alert=True
        )
        return
    await call.answer("Yuklanmoqda...")
    try:
        query = user_modes.get(user_id)
        if not query:
            await call.message.answer(
                "Qidiruvni qayta boshlang.",
                reply_markup=main_keyboard()
            )
            return
        results, next_token, _ = await youtube_search(
            query,
            tokens[page]
        )
        youtube_results[user_id] = results
        if next_token:
            tokens[page + 1] = next_token
        await show_youtube_results(
            call.message,
            user_id,
            page
        )
    except Exception:
        logger.exception("Sahifa xatosi")
        await call.message.answer(
            "Sahifani yuklashda xatolik yuz berdi."
        )
# =========================
# QO'SHIQ TANLASH
# =========================
@dp.callback_query(F.data.startswith("yt:"))
async def youtube_track(call: CallbackQuery):
    user_id = call.from_user.id
    try:
        index = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Tanlov noto'g'ri.", show_alert=True)
        return
    results = youtube_results.get(user_id, [])
    if index < 0 or index >= len(results):
        await call.answer(
            "Natija topilmadi.",
            show_alert=True
        )
        return
    item = results[index]
    title = item.get("title", "Noma'lum")
    artist = item.get("artist", "Noma'lum")
    duration = item.get("duration", "Noma'lum")
    thumbnail = item.get("thumbnail")
    video_url = item.get("url")
    caption = (
        f"🎵 <b>{title}</b>\n\n"
        f"👤 {artist}\n"
        f"⏱ {duration}\n\n"
        "🎧 <b>ASAVED MUSIC</b>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶️ YouTube'da tinglash",
                    url=video_url
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Bosh menyu",
                    callback_data="home"
                )
            ]
        ]
    )
    await call.answer()
    try:
        if thumbnail:
            await call.message.answer_photo(
                photo=thumbnail,
                caption=caption,
                reply_markup=keyboard
            )
        else:
            await call.message.answer(
                caption,
                reply_markup=keyboard
            )
    except Exception:
        logger.exception("Rasm yuborish xatosi")
        await call.message.answer(
            caption,
            reply_markup=keyboard
        )
# =========================
# RENDER WEBHOOK
# =========================
async def health(request):
    return web.json_response({
        "status": "ok",
        "bot": "ASAVED"
    })
async def on_startup(app):
    webhook_url = (
        f"{RENDER_URL.rstrip('/')}/{WEBHOOK_PATH}"
    )
    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types()
    )
    logger.info("ASAVED BOT ISHGA TUSHDI")
    logger.info("WEBHOOK: %s", webhook_url)
async def on_shutdown(app):
    await bot.session.close()
def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET
    ).register(
        app,
        path=f"/{WEBHOOK_PATH}"
    )
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_shutdown)
    web.run_app(
        app,
        host="0.0.0.0",
        port=PORT
    )
if __name__ == "__main__":
    main()
