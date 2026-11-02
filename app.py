
import asyncio
import os
import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.enums import ChatMemberStatus
from aiogram.client.default import DefaultBotProperties

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL = "@akoshsaved"
CHANNEL_URL = "https://t.me/akoshsaved"

bot = Bot(
    TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)
dp = Dispatcher()

users_results = {}


def sub_buttons():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📢 Kanalga obuna bo‘lish",
            url=CHANNEL_URL
        )],
        [InlineKeyboardButton(
            text="✅ Tekshirish",
            callback_data="check"
        )]
    ])


async def check_sub(user_id):
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status in [
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR
        ] or (
            member.status == ChatMemberStatus.RESTRICTED
            and member.is_member
        )
    except Exception:
        return False


async def search_music(query):
    url = "https://itunes.apple.com/search"
    params = {
        "term": query,
        "entity": "song",
        "limit": 50
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as response:
            data = await response.json()
            return data.get("results", [])


@dp.message(CommandStart())
async def start(message: Message):
    if not await check_sub(message.from_user.id):
        await message.answer(
            "🎵 Botdan foydalanish uchun kanalga obuna bo‘ling!",
            reply_markup=sub_buttons()
        )
        return

    await message.answer(
        "👋 <b>Salom! AUDIO SAVED BOT</b>\n\n"
        "🎧 Qo‘shiq nomini yoki ijrochi ismini yuboring.\n\n"
        "🔎 Men sizga mos musiqalarni topaman!"
    )


@dp.callback_query(F.data == "check")
async def check_button(call: CallbackQuery):
    if await check_sub(call.from_user.id):
        await call.message.answer(
            "✅ Obuna tasdiqlandi!\n"
            "🎵 Endi qo‘shiq nomini yuboring."
        )
    else:
        await call.answer(
            "❌ Avval kanalga obuna bo‘ling!",
            show_alert=True
        )


@dp.message(F.text)
async def music_search(message: Message):
    if not await check_sub(message.from_user.id):
        await message.answer(
            "Avval kanalga obuna bo‘ling!",
            reply_markup=sub_buttons()
        )
        return

    query = message.text.strip()

    if query.startswith("/"):
        return

    if len(query) < 2:
        await message.answer("Qo‘shiq nomini yozing.")
        return

    wait = await message.answer("🔎 Musiqa qidirilmoqda...")

    try:
        results = await search_music(query)
    except Exception:
        await wait.edit_text("❌ Qidiruvda xatolik. Qayta urinib ko‘ring.")
        return

    if not results:
        await wait.edit_text("😔 Musiqa topilmadi.")
        return

    users_results[message.from_user.id] = results

    await show_results(
        wait,
        message.from_user.id,
        0
    )


async def show_results(message, user_id, page):
    results = users_results.get(user_id, [])
    start = page * 10
    current = results[start:start + 10]

    text = "🎵 <b>MUSIQALAR TOPILDI</b>\n\n"

    buttons = []

    for i, song in enumerate(current, start=start):
        name = song.get("trackName", "Unknown")
        artist = song.get("artistName", "Unknown")
        duration = song.get("trackTimeMillis", 0)

        seconds = duration // 1000
        time = f"{seconds // 60}:{seconds % 60:02d}"

        text += (
            f"<b>{i + 1}.</b> {name}\n"
            f"🎤 {artist} | ⏱ {time}\n\n"
        )

        buttons.append([
            InlineKeyboardButton(
                text=f"🎧 {i + 1}",
                callback_data=f"song:{i}"
            )
        ])

    nav = []

    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"page:{page - 1}"
            )
        )

    if start + 10 < len(results):
        nav.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"page:{page + 1}"
            )
        )

    if nav:
        buttons.append(nav)

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.edit_text(
        text,
        reply_markup=keyboard
    )


@dp.callback_query(F.data.startswith("page:"))
async def next_page(call: CallbackQuery):
    if not await check_sub(call.from_user.id):
        await call.message.answer(
            "Obuna bo‘ling!",
            reply_markup=sub_buttons()
        )
        await call.answer()
        return

    page = int(call.data.split(":")[1])

    if call.from_user.id not in users_results:
        await call.answer("Qayta qidiring.", show_alert=True)
        return

    await show_results(
        call.message,
        call.from_user.id,
        page
    )

    await call.answer()


@dp.callback_query(F.data.startswith("song:"))
async def select_song(call: CallbackQuery):
    if not await check_sub(call.from_user.id):
        await call.answer("Avval obuna bo‘ling!", show_alert=True)
        return

    results = users_results.get(call.from_user.id, [])

    index = int(call.data.split(":")[1])

    if index >= len(results):
        await call.answer("Musiqa topilmadi.")
        return

    song = results[index]

    name = song.get("trackName", "Unknown")
    artist = song.get("artistName", "Unknown")
    album = song.get("collectionName", "Unknown")
    link = song.get("trackViewUrl")
    image = song.get("artworkUrl100")

    text = (
        f"🎵 <b>{name}</b>\n\n"
        f"🎤 Ijrochi: {artist}\n"
        f"💿 Albom: {album}\n\n"
        "🎧 Rasmiy tinglash havolasi:"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🎧 Tinglash",
            url=link
        )]
    ]) if link else None

    if image:
        await call.message.answer_photo(
            photo=image,
            caption=text,
            reply_markup=keyboard
        )
    else:
        await call.message.answer(
            text,
            reply_markup=keyboard
        )

    await call.answer()


async def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN kiritilmagan!")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
