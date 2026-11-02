import asyncio
import logging
import os
from html import escape

import aiohttp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL = os.getenv("CHANNEL_USERNAME", "@akoshsaved").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/akoshsaved").strip()
PER_PAGE = 10
SEARCH_URL = "https://itunes.apple.com/search"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("music-bot")
dp = Dispatcher()
user_results: dict[int, list[dict]] = {}


def sub_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Kanalga obuna bo‘lish", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_sub")]
    ])


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        if member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR
        }:
            return True
        return (
            member.status == ChatMemberStatus.RESTRICTED
            and bool(getattr(member, "is_member", False))
        )
    except Exception:
        logger.exception("Kanal obunasini tekshirishda xato")
        return False


async def search_tracks(query: str) -> list[dict]:
    params = {"term": query, "entity": "song", "limit": 50, "country": "us"}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(SEARCH_URL, params=params) as response:
            response.raise_for_status()
            data = await response.json()
            return data.get("results", [])


def results_keyboard(items: list[dict], page: int):
    start = page * PER_PAGE
    rows = []
    for i, track in enumerate(items[start:start + PER_PAGE], start=start):
        title = track.get("trackName", "Noma’lum")
        artist = track.get("artistName", "Noma’lum")
        rows.append([InlineKeyboardButton(
            text=f"{i + 1}. {title} — {artist}"[:60],
            callback_data=f"song:{i}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"page:{page-1}"))
    if start + PER_PAGE < len(items):
        nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"page:{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_results(message: Message, user_id: int, page: int):
    items = user_results.get(user_id, [])
    pages = (len(items) + PER_PAGE - 1) // PER_PAGE
    if not items or page < 0 or page >= pages:
        await message.answer("Natijalar topilmadi. Qayta qidirib ko‘ring.")
        return
    first = page * PER_PAGE + 1
    last = min((page + 1) * PER_PAGE, len(items))
    await message.answer(
        f"🎵 <b>Musiqalar topildi</b>\n"
        f"🔎 Natijalar: {first}–{last} / {len(items)}\n\n"
        "Kerakli qo‘shiqni tanlang:",
        reply_markup=results_keyboard(items, page)
    )


@dp.message(CommandStart())
async def start_handler(message: Message, bot: Bot):
    if not await is_subscribed(bot, message.from_user.id):
        await message.answer(
            "🎵 Botdan foydalanish uchun avval kanalimizga obuna bo‘ling:",
            reply_markup=sub_keyboard()
        )
        return
    await message.answer(
        "👋 <b>Music Search Bot'ga xush kelibsiz!</b>\n\n"
        "🎧 Qo‘shiq yoki ijrochi nomini yuboring.\n"
        "Masalan: <i>Jaloliddin Ahmadaliyev</i>"
    )


@dp.callback_query(F.data == "check_sub")
async def check_sub_handler(call: CallbackQuery, bot: Bot):
    if await is_subscribed(bot, call.from_user.id):
        await call.message.answer("✅ Obuna tasdiqlandi! Endi qo‘shiq nomini yuboring.")
        await call.answer("Obuna tasdiqlandi!")
    else:
        await call.answer("Avval kanalga obuna bo‘ling!", show_alert=True)


@dp.message(F.text)
async def search_handler(message: Message, bot: Bot):
    if not await is_subscribed(bot, message.from_user.id):
        await message.answer("Davom etish uchun kanalga obuna bo‘ling:", reply_markup=sub_keyboard())
        return

    query = message.text.strip()
    if query.startswith("/"):
        return
    if len(query) < 2:
        await message.answer("Qo‘shiq yoki ijrochi nomini yozing.")
        return

    status = await message.answer("🔎 Musiqa qidirilmoqda...")
    try:
        items = await search_tracks(query)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.exception("Musiqa qidiruv API xatosi")
        await status.edit_text("❌ Qidiruv xizmatida xatolik. Keyinroq urinib ko‘ring.")
        return
    except Exception:
        logger.exception("Kutilmagan qidiruv xatosi")
        await status.edit_text("❌ Kutilmagan xatolik yuz berdi.")
        return

    if not items:
        await status.edit_text("😕 Bu nom bo‘yicha musiqa topilmadi.")
        return

    user_results[message.from_user.id] = items
    await status.delete()
    await show_results(message, message.from_user.id, 0)


@dp.callback_query(F.data.startswith("page:"))
async def page_handler(call: CallbackQuery, bot: Bot):
    if not await is_subscribed(bot, call.from_user.id):
        await call.message.answer("Kanalga obuna bo‘ling:", reply_markup=sub_keyboard())
        await call.answer()
        return
    try:
        page = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Sahifa noto‘g‘ri.")
        return

    items = user_results.get(call.from_user.id, [])
    pages = (len(items) + PER_PAGE - 1) // PER_PAGE
    if page < 0 or page >= pages:
        await call.answer("Qidiruv muddati tugagan. Qayta qidiring.", show_alert=True)
        return

    first = page * PER_PAGE + 1
    last = min((page + 1) * PER_PAGE, len(items))
    await call.message.edit_text(
        f"🎵 <b>Musiqalar topildi</b>\nNatijalar: {first}–{last} / {len(items)}\n\n"
        "Kerakli qo‘shiqni tanlang:",
        reply_markup=results_keyboard(items, page)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("song:"))
async def song_handler(call: CallbackQuery, bot: Bot):
    if not await is_subscribed(bot, call.from_user.id):
        await call.message.answer("Kanalga obuna bo‘ling:", reply_markup=sub_keyboard())
        await call.answer()
        return

    items = user_results.get(call.from_user.id, [])
    try:
        index = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Tanlov noto‘g‘ri.")
        return
    if index < 0 or index >= len(items):
        await call.answer("Natija topilmadi. Qayta qidiring.", show_alert=True)
        return

    track = items[index]
    title = escape(track.get("trackName", "Noma’lum"))
    artist = escape(track.get("artistName", "Noma’lum"))
    album = escape(track.get("collectionName", "Noma’lum"))
    duration_ms = track.get("trackTimeMillis")
    duration = ""
    if isinstance(duration_ms, int) and duration_ms > 0:
        seconds = duration_ms // 1000
        duration = f"\n⏱ Davomiyligi: {seconds // 60}:{seconds % 60:02d}"

    text = f"🎵 <b>{title}</b>\n🎤 Ijrochi: {artist}\n💿 Albom: {album}{duration}\n\n🔗 Rasmiy tinglash sahifasi:"
    link = track.get("trackViewUrl")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎧 Tinglash", url=link)]
    ]) if link else None

    artwork = track.get("artworkUrl100")
    try:
        if artwork:
            await call.message.answer_photo(photo=artwork, caption=text, reply_markup=keyboard)
        else:
            await call.message.answer(text, reply_markup=keyboard)
    except Exception:
        logger.exception("Trek ma’lumotini yuborishda xato")
        await call.message.answer(text, reply_markup=keyboard)
    await call.answer()


async def main():
    if not TOKEN:
        raise RuntimeError("Render Environment Variables ichida BOT_TOKEN qo‘shilmagan.")

    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        # Webhook va polling bir vaqtda ishlamaydi: eski webhookni o‘chiramiz.
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook o‘chirildi. Polling ishga tushmoqda.")

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types()
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
