
import random
import secrets
import sqlite3
from pathlib import Path

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)

game_router = Router()

DB_PATH = Path("asaved_users.db")

# Faol o'yin xonalari
rooms = {}

GAMES = {
    "quiz": "🧠 Kim meni yaxshi biladi?",
    "fast": "⚡ Tezkor savol-javob",
    "who": "🎭 Men kimman?",
    "fun": "🤣 Kulgi jangi",
    "more": "😂 Kim ko‘proq?",
}

QUESTIONS = {
    "quiz": [
        ("Do‘stlikda eng muhim narsa nima?", ["Ishonch", "Pul", "Telefon", "Sovg‘a"], 0),
        ("Do‘sting sirini aytsa nima qilasan?", ["Saqlayman", "Hammaga aytaman", "Unutaman", "Kulaman"], 0),
        ("Yaxshi do‘st qanday bo‘ladi?", ["Sodiq", "Maqtanchoq", "Yolg‘onchi", "Dangasa"], 0),
    ],
    "fast": [
        ("O‘zbekiston poytaxti?", ["Toshkent", "Samarqand", "Buxoro", "Andijon"], 0),
        ("Bir haftada nechta kun bor?", ["5", "6", "7", "8"], 2),
        ("Quyosh qaysi tomondan chiqadi?", ["G‘arb", "Sharq", "Shimol", "Janub"], 1),
        ("Bir soatda necha daqiqa bor?", ["30", "45", "60", "100"], 2),
    ],
    "who": [
        ("Men qaysi hayvonman? Miyovlayman.", ["It", "Mushuk", "Sher", "Ot"], 1),
        ("Men uchaman, lekin samolyot emasman.", ["Qush", "Baliq", "Mushuk", "Sigir"], 0),
        ("Menda toj bor, lekin qirol emasman.", ["Xo‘roz", "Ot", "Fil", "Bo‘ri"], 0),
    ],
    "fun": [
        ("Do‘sting uxlab qolsa, nima qilasan?", ["Rasmga olaman", "Uyg‘otaman", "Ketaman", "Kutaman"], 0),
        ("Telefoning 1% bo‘lsa nima qilasan?", ["Zaryad qidiraman", "O‘ynayman", "Video olaman", "Uxlayman"], 0),
        ("Do‘sting ovqat olib bersa?", ["Rahmat aytaman", "Hammasini yeyman", "Bo‘lishaman", "Qochaman"], 2),
    ],
    "more": [
        ("Kim ko‘proq kech qoladi?", ["Men", "Do‘stim", "Ikkalamiz", "Hech kim"], 0),
        ("Kim ko‘proq telefon ishlatadi?", ["Men", "Do‘stim", "Ikkalamiz", "Hech kim"], 2),
        ("Kim ko‘proq kuladi?", ["Men", "Do‘stim", "Ikkalamiz", "Hech kim"], 2),
    ],
}


def init_games_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS game_scores (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                points INTEGER DEFAULT 0
            )
        """)
        db.commit()


def add_points(user_id, name, points):
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            INSERT INTO game_scores(user_id, name, points)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name,
                points=points + excluded.points
        """, (user_id, name, points))
        db.commit()


def get_top():
    with sqlite3.connect(DB_PATH) as db:
        return db.execute("""
            SELECT name, points FROM game_scores
            ORDER BY points DESC LIMIT 10
        """).fetchall()


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 O‘yin xonasi yaratish", callback_data="game_create")],
        [InlineKeyboardButton(text="🏆 Reyting", callback_data="game_top")],
    ])


def games_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"game_type:{key}")]
        for key, name in GAMES.items()
    ])


def room_keyboard(code, owner=False):
    rows = [
        [InlineKeyboardButton(
            text="👥 O‘yinga qo‘shilish",
            callback_data=f"game_join:{code}"
        )]
    ]
    if owner:
        rows.append([
            InlineKeyboardButton(
                text="🚀 O‘yinni boshlash",
                callback_data=f"game_start:{code}"
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@game_router.message(Command("games"))
async def games_command(message: Message):
    init_games_db()
    await message.answer(
        "🎮 <b>DO‘STLAR BATTLE</b>\n\n"
        "Do‘stlaring bilan o‘ynash uchun xona yarat!",
        reply_markup=main_keyboard()
    )


@game_router.callback_query(F.data == "game_create")
async def create_room(callback: CallbackQuery):
    if not callback.message:
        await callback.answer()
        return

    code = secrets.token_urlsafe(5)
    rooms[code] = {
        "owner": callback.from_user.id,
        "players": {},
        "chat_id": callback.message.chat.id,
        "game": None,
        "question_index": 0,
        "questions": [],
        "answered": set(),
        "finished": False,
    }

    me = await callback.bot.get_me()
    invite = f"https://t.me/{me.username}?start=game_{code}"

    await callback.answer("O‘yin xonasi yaratildi!")
    await callback.message.answer(
        f"🎮 <b>O‘YIN XONASI TAYYOR!</b>\n\n"
        f"🔑 Xona kodi: <code>{code}</code>\n"
        f"👤 Tashkilotchi: {callback.from_user.full_name}\n\n"
        f"🔗 Do‘stlaringni taklif qil:\n{invite}\n\n"
        "Do‘stlaring qo‘shilgach, o‘yinni boshlashing mumkin.",
        reply_markup=room_keyboard(code, True)
    )


async def join_room(message: Message, code: str):
    room = rooms.get(code)

    if not room:
        await message.answer("❌ Bu xona topilmadi yoki bot qayta ishga tushgan.")
        return True

    if room["finished"] or room["game"]:
        await message.answer("❌ Bu o‘yin allaqachon boshlangan.")
        return True

    room["players"][message.from_user.id] = {
        "name": message.from_user.full_name,
        "score": 0,
    }

    await message.answer(
        f"✅ O‘yinga qo‘shildingiz!\n"
        f"👥 Ishtirokchilar: {len(room['players'])}",
        reply_markup=room_keyboard(code)
    )

    await message.bot.send_message(
        room["chat_id"],
        f"👤 <b>{message.from_user.full_name}</b> xonaga qo‘shildi!"
    )
    return True


async def handle_game_start(message: Message):
    if not message.text:
        return False

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].startswith("game_"):
        return False

    code = parts[1][5:]
    return await join_room(message, code)


@game_router.message(Command("join"))
async def join_command(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Ishlatish: /join XONA_KODI")
        return
    await join_room(message, parts[1].strip())


@game_router.callback_query(F.data.startswith("game_join:"))
async def join_callback(callback: CallbackQuery):
    code = callback.data.split(":", 1)[1]
    room = rooms.get(code)

    if not room:
        await callback.answer("Xona topilmadi.", show_alert=True)
        return

    if room["game"] or room["finished"]:
        await callback.answer("O‘yin allaqachon boshlangan.", show_alert=True)
        return

    room["players"][callback.from_user.id] = {
        "name": callback.from_user.full_name,
        "score": 0,
    }

    await callback.answer("Siz o‘yinga qo‘shildingiz!")
    await callback.bot.send_message(
        room["chat_id"],
        f"👤 {callback.from_user.full_name} o‘yinga qo‘shildi!"
    )


@game_router.callback_query(F.data == "game_top")
async def show_top(callback: CallbackQuery):
    rows = get_top()
    if not rows:
        result = "🏆 Hali reyting mavjud emas."
    else:
        result = "🏆 <b>DO‘STLAR BATTLE TOP-10</b>\n\n"
        medals = ["🥇", "🥈", "🥉"]
        for i, (name, points) in enumerate(rows, 1):
            medal = medals[i-1] if i <= 3 else f"{i}."
            result += f"{medal} {name} — <b>{points}</b> ball\n"

    await callback.answer()
    if callback.message:
        await callback.message.answer(result)


@game_router.callback_query(F.data.startswith("game_type:"))
async def choose_game(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    if key not in GAMES:
        await callback.answer("O‘yin topilmadi.", show_alert=True)
        return

    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"🎮 Tanlangan o‘yin: {GAMES[key]}\n\n"
            "O‘yin xonasini yarating va do‘stlaringizni taklif qiling.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="➕ Xona yaratish",
                    callback_data="game_create"
                )]
            ])
        )


@game_router.callback_query(F.data.startswith("game_start:"))
async def start_game(callback: CallbackQuery):
    code = callback.data.split(":", 1)[1]
    room = rooms.get(code)

    if not room:
        await callback.answer("Xona topilmadi.", show_alert=True)
        return

    if callback.from_user.id != room["owner"]:
        await callback.answer("Faqat xona egasi boshlashi mumkin.", show_alert=True)
        return

    if len(room["players"]) < 2:
        await callback.answer("Kamida 2 o‘yinchi kerak!", show_alert=True)
        return

    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🎮 Qaysi o‘yinni boshlaymiz?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=name,
                    callback_data=f"play:{code}:{key}"
                )]
                for key, name in GAMES.items()
            ])
        )


@game_router.callback_query(F.data.startswith("play:"))
async def play_game(callback: CallbackQuery):
    _, code, key = callback.data.split(":")
    room = rooms.get(code)

    if not room or key not in QUESTIONS:
        await callback.answer("O‘yin xonasi topilmadi.", show_alert=True)
        return

    if callback.from_user.id != room["owner"]:
        await callback.answer("Faqat xona egasi o‘yinni tanlaydi.", show_alert=True)
        return

    room["game"] = key
    room["questions"] = random.sample(
        QUESTIONS[key],
        min(5, len(QUESTIONS[key]))
    )
    room["question_index"] = 0
    room["answered"] = set()

    await callback.answer("O‘yin boshlandi!")
    await send_question(callback.bot, code)


async def send_question(bot, code):
    room = rooms.get(code)
    if not room:
        return

    index = room["question_index"]
    if index >= len(room["questions"]):
        await finish_game(bot, code)
        return

    question, options, correct = room["questions"][index]
    room["answered"] = set()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=option,
            callback_data=f"answer:{code}:{i}"
        )]
        for i, option in enumerate(options)
    ])

    await bot.send_message(
        room["chat_id"],
        f"🎮 <b>{GAMES[room['game']]}</b>\n"
        f"📝 Savol {index + 1}/{len(room['questions'])}\n\n"
        f"<b>{question}</b>",
        reply_markup=keyboard
    )


@game_router.callback_query(F.data.startswith("answer:"))
async def answer_question(callback: CallbackQuery):
    _, code, answer = callback.data.split(":")
    room = rooms.get(code)

    if not room or room["finished"] or not room["game"]:
        await callback.answer("O‘yin tugagan.", show_alert=True)
        return

    user_id = callback.from_user.id

    if user_id not in room["players"]:
        await callback.answer("Siz bu o‘yin ishtirokchisi emassiz.", show_alert=True)
        return

    if user_id in room["answered"]:
        await callback.answer("Javobingiz qabul qilingan.", show_alert=True)
        return

    index = room["question_index"]
    question, options, correct = room["questions"][index]
    answer = int(answer)

    if answer < 0 or answer >= len(options):
        await callback.answer("Noto‘g‘ri javob.", show_alert=True)
        return

    room["answered"].add(user_id)

    if answer == correct:
        room["players"][user_id]["score"] += 10
        await callback.answer("✅ To‘g‘ri! +10 ball")
    else:
        await callback.answer("❌ Noto‘g‘ri!")

    if len(room["answered"]) >= len(room["players"]):
        room["question_index"] += 1
        await send_question(callback.bot, code)


async def finish_game(bot, code):
    room = rooms.get(code)
    if not room:
        return

    room["finished"] = True
    ranking = sorted(
        room["players"].items(),
        key=lambda item: item[1]["score"],
        reverse=True
    )

    text = "🏆 <b>O‘YIN YAKUNLANDI!</b>\n\n"

    for i, (user_id, player) in enumerate(ranking, 1):
        points = player["score"]
        text += f"{i}. {player['name']} — <b>{points}</b> ball\n"
        if points:
            add_points(user_id, player["name"], points)

    text += "\n🎮 Yana o‘ynash uchun /games buyrug‘ini yuboring."

    await bot.send_message(room["chat_id"], text)
    rooms.pop(code, None)
