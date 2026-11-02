import os
import asyncio
import logging
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
# =========================
# SOZLAMALAR
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", "10000"))
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ASAVED")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN kiritilmagan!")
if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
    raise RuntimeError("WEBHOOK_SECRET kamida 16 belgidan iborat bo‘lsin!")
if not RENDER_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL topilmadi!")
# =========================
# BOT
# =========================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
# =========================
# JAMENDO QIDIRUV
# =========================
async def jamendo_search(query):
    if not JAMENDO_CLIENT_ID:
        raise RuntimeError("JAMENDO_CLIENT_ID kiritilmagan")
    api_url = "https://api.jamendo.com/v3.0/tracks/"
    params = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        "limit": 10,
        "search": query,
        "type": "single albumtrack",
        "include": "musicinfo",
        "order": "relevance"
    }
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            api_url,
            params=params
        ) as response:
            data = await response.json(content_type=None)
            logger.info("JAMENDO STATUS: %s", response.status)
            logger.info("JAMENDO HEADERS: %s", data.get("headers"))
            if response.status != 200:
                raise RuntimeError("Jamendo HTTP xatosi")
            headers = data.get("headers", {})
            if headers.get("status") != "success":
                raise RuntimeError(
                    "Jamendo API: " +
                    str(headers.get("error_message"))
                )
            results = data.get("results", [])
            logger.info("JAMENDO RESULTS: %s", len(results))
            return results
# =========================
# START
# =========================
@dp.message(CommandStart())
async def start(message: Message):
    text = (
        "🎵 <b>ASAVED MUSIC BOT</b>\n\n"
        "Assalomu alaykum!\n"
        "Bot orqali musiqa qidirishingiz mumkin.\n\n"
        "🔎 Qo‘shiq yoki ijrochi nomini yuboring.\n\n"
        "Masalan:\n"
        "• Imagine Dragons\n"
        "• Alan Walker\n"
        "• Electronic\n\n"
        "🎧 Musiqa qidirishni boshlang!"
    )
    await message.answer(text)
# =========================
# HELP
# =========================
@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "🎵 <b>ASAVED yordam</b>\n\n"
        "/start — Botni boshlash\n"
        "/help — Yordam\n"
        "/search nom — Musiqa qidirish\n\n"
        "Oddiy xabar yuborib ham musiqa qidirishingiz mumkin."
    )
# =========================
# SEARCH COMMAND
# =========================
@dp.message(Command("search"))
async def search_command(message: Message):
    query = message.text.replace("/search", "", 1).strip()
    if not query:
        await message.answer(
            "🔎 Qidirish uchun qo‘shiq nomini yozing.\n"
            "Masalan: /search Alan Walker"
        )
        return
    await search_music(message, query)
# =========================
# MUSIQA QIDIRISH
# =========================
async def search_music(message: Message, query: str):
    loading = await message.answer("🔍 Musiqa qidirilmoqda...")
    try:
        results = await jamendo_search(query)
        if not results:
            await loading.edit_text(
                "😕 Hech qanday musiqa topilmadi.\n\n"
                "Boshqa nom bilan qidirib ko‘ring."
            )
            return
        await loading.edit_text(
            f"🎵 <b>{query}</b> bo‘yicha natijalar:\n"
            f"Topildi: {len(results)} ta"
        )
        for index, track in enumerate(results, start=1):
            name = track.get("name", "Noma'lum")
            artist = track.get("artist_name", "Noma'lum")
            album = track.get("album_name", "Noma'lum")
            duration = int(track.get("duration", 0) or 0)
            share_url = track.get("shareurl")
            minutes = duration // 60
            seconds = duration % 60
            text = (
                f"🎧 <b>{index}. {name}</b>\n\n"
                f"👤 Ijrochi: {artist}\n"
                f"💿 Albom: {album}\n"
                f"⏱ Davomiyligi: {minutes}:{seconds:02d}\n\n"
                f"🌐 <i>Rasmiy Jamendo havolasi:</i>"
            )
            buttons = []
            if share_url:
                buttons.append([
                    InlineKeyboardButton(
                        text="🎵 Qo‘shiqni ochish",
                        url=share_url
                    )
                ])
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=buttons
            ) if buttons else None
            await message.answer(
                text,
                reply_markup=keyboard
            )
    except Exception:
        logger.exception("Musiqa qidirishda xato")
        await loading.edit_text(
            "⚠️ Qidirishda xatolik yuz berdi.\n"
            "Keyinroq qayta urinib ko‘ring."
        )
# =========================
# ODDIY XABARLAR
# =========================
@dp.message(F.text)
async def text_handler(message: Message):
    query = message.text.strip()
    if not query:
        return
    await search_music(message, query)
# =========================
# TELEGRAM WEBHOOK
# =========================
async def telegram_webhook(request: web.Request):
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
        logger.exception("Webhook update xatosi")
        return web.Response(status=500)
# =========================
# HEALTH CHECK
# =========================
async def health(request):
    return web.json_response({
        "status": "ok",
        "bot": "ASAVED"
    })
# =========================
# ISHGA TUSHIRISH
# =========================
async def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post(
        f"/{WEBHOOK_PATH}",
        telegram_webhook
    )
    webhook_url = (
        f"{RENDER_URL.rstrip('/')}/{WEBHOOK_PATH}"
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )
    await site.start()
    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True
    )
    logger.info("ASAVED BOT ISHGA TUSHDI")
    logger.info("WEBHOOK: %s", webhook_url)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await bot.session.close()
if __name__ == "__main__":
    asyncio.run(main())
