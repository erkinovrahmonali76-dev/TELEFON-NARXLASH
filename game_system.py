import os
import random
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)

game_router = Router(name="asaved_games")
DB_PATH = Path(os.getenv("DB_PATH", "asaved_users.db"))

# Xonalar RAM'da turadi. Render qayta ishga tushsa faol o'yinlar yopiladi,
# lekin ballar SQLite bazasida saqlanadi.
rooms: dict[str, dict[str, Any]] = {}

GAMES = {
    "quiz": "🧠 Bilimlar jangi",
    "fast": "⚡ Tezkor savol-javob",
    "who": "🎭 Men kimman?",
    "fun": "🤣 Kulgi jangi",
    "more": "😂 Kim ko‘proq?",
}

QUESTIONS = {
    "quiz": [
        ("O‘zbekiston poytaxti qaysi shahar?", ["Toshkent", "Samarqand", "Buxoro", "Xiva"], 0),
        ("Bir haftada nechta kun bor?", ["5", "6", "7", "8"], 2),
        ("Suvning kimyoviy formulasi qaysi?", ["CO2", "H2O", "O2", "NaCl"], 1),
        ("Quyosh qaysi tomondan chiqadi?", ["G‘arb", "Sharq", "Shimol", "Janub"], 1),
        ("Bir soatda necha daqiqa bor?", ["30", "45", "60", "90"], 2),
        ("O‘zbekiston bayrog‘ida nechta rang bor?", ["3", "4", "5", "6"], 1),
    ],
    "fast": [
        ("9 × 7 nechchi?", ["56", "63", "72", "69"], 1),
        ("Eng katta okean qaysi?", ["Atlantika", "Hind", "Tinch", "Shimoliy Muz"], 2),
        ("Bir kunda necha soat bor?", ["12", "18", "24", "48"], 2),
        ("Alifboda A harfidan keyin qaysi harf keladi?", ["B", "D", "E", "C"], 0),
        ("100 ning yarmi nechchi?", ["25", "40", "50", "75"], 2),
    ],
    "who": [
        ("Miyovlayman, sut ichishni yaxshi ko‘raman. Men kimman?", ["It", "Mushuk", "Ot", "Sigir"], 1),
        ("Qanotim bor, osmonda uchaman. Men kimman?", ["Baliq", "Qush", "Mushuk", "Ilon"], 1),
        ("Uzun quloqlarim bor, sabzini yaxshi ko‘raman. Men kimman?", ["Quyon", "Sher", "Ayiq", "Bo‘ri"], 0),
        ("O‘rmonda shoh deb ataladigan hayvon kim?", ["Fil", "Sher", "Maymun", "Tulki"], 1),
    ],
    "fun": [
        ("Do‘sting uxlab qolsa, eng kulgili nima qilasan?", ["Rasmga tushirasan", "Uyg‘otasan", "Choy berasan", "Kutasan"], 0),
        ("Telefoning 1% bo‘lsa, birinchi nima qilasan?", ["Zaryad qidirasan", "Video olasan", "O‘yin o‘ynaysan", "Yana 1 soat ishlatasan"], 0),
        ("Do‘sting ovqatingdan so‘ramay olsa nima deysan?", ["Yoqimli ishtaha", "Bu meniki!", "Yana ol", "Indamaysan"], 1),
        ("Darsga kech qolsang, eng mashhur bahona?", ["Transport kutdim", "Soat ishlamadi", "Yo‘l uzoq edi", "Uyg‘onolmadim"], 3),
    ],
    "more": [
        ("Kim ko‘proq kuladi?", ["Men", "Do‘stim", "Ikkalamiz", "Hech kim"], 2),
        ("Kim ko‘proq telefon ishlatadi?", ["Men", "Do‘stim", "Ikkalamiz", "Hech kim"], 2),
        ("Kim ko‘proq kech qoladi?", ["Men", "Do‘stim", "Ikkalamiz", "Hech kim"], 2),
        ("Kim ko‘proq ovqat yeydi?", ["Men", "Do‘stim", "Ikkalamiz", "Hech kim"], 2),
    ],
}


def init_games_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=30) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS game_scores (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                games_played INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.commit()


def add_points(user_id: int, name: str, points: int) -> None:
    with sqlite3.connect(DB_PATH, timeout=30) as db:
        db.execute("""
            INSERT INTO game_scores(user_id, name, points, games_played)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name,
                points=game_scores.points + excluded.points,
                games_played=game_scores.games_played + 1
        """, (user_id, name[:100], points))
        db.commit()


def get_top() -> list[tuple[str, int, int]]:
    with sqlite3.connect(DB_PATH, timeout=30) as db:
        return db.execute("""
            SELECT name, points, games_played
            FROM game_scores
            ORDER BY points DESC, games_played DESC
            LIMIT 10
        """).fetchall()


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 O‘yin xonasi yaratish", callback_data="game_create")],
        [InlineKeyboardButton(text="🏆 Reyting", callback_data="game_top")],
    ])


def room_keyboard(code: str, owner: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text="👥 Xonaga qo‘shilish",
        callback_data=f"game_join:{code}"
    )]]
    if owner:
        rows.append([InlineKeyboardButton(
            text="🚀 O‘yinni boshlash",
            callback_data=f"game_start:{code}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _announce_join(bot, room: dict, name: str) -> None:
    try:
        await bot.send_message(
            room["chat_id"],
            f"👤 <b>{name}</b> o‘yin xonasiga qo‘shildi!\n"
            f"👥 Ishtirokchilar: <b>{len(room['players'])}</b>"
        )
    except Exception:
        pass


async def add_player(bot, code: str, user, private_started: bool = False) -> tuple[bool, str]:
    room = rooms.get(code)
    if not room:
        return False, "❌ Xona topilmadi. Balki bot qayta ishga tushgandir."
    if room["game"] is not None or room["finished"]:
        return False, "❌ Bu xonada o‘yin allaqachon boshlangan."
    if user.id in room["players"]:
        room["players"][user.id]["private_started"] |= private_started
        return True, "Siz allaqachon xonadasiz."

    if len(room["players"]) >= 30:
        return False, "❌ Xonada 30 kishilik limit to‘ldi."

    room["players"][user.id] = {
        "name": user.full_name[:100],
        "score": 0,
        "private_started": private_started,
    }
    await _announce_join(bot, room, user.full_name)
    return True, f"✅ Xonaga qo‘shildingiz!\n👥 Ishtirokchilar: {len(room['players'])}"


@game_router.message(Command("games"))
async def games_command(message: Message):
    init_games_db()
    await message.answer(
        "🎮 <b>DO‘STLAR BATTLE</b>\n\n"
        "Do‘stlaring bilan o‘ynash uchun xona yarat.\n"
        "Xonani shaxsiy chatda yoki guruhda ochishing mumkin.",
        reply_markup=home_keyboard()
    )


@game_router.message(CommandStart(deep_link=True))
async def game_deep_link(message: Message):
    # Faqat /start game_CODE havolasini ushlaymiz.
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].startswith("game_"):
        return
    code = parts[1][5:]
    ok, result = await add_player(message.bot, code, message.from_user, True)
    if not ok:
        await message.answer(result)
        return
    room = rooms[code]
    await message.answer(
        f"{result}\n\n🎮 <b>{GAMES.get(room['selected_game'], 'Do‘stlar Battle')}</b>\n"
        "O‘yin boshlanganda savollar shu shaxsiy chatga ham keladi.",
        reply_markup=room_keyboard(code)
    )


@game_router.callback_query(F.data == "game_create")
async def create_room(callback: CallbackQuery):
    if not callback.message:
        await callback.answer()
        return

    code = secrets.token_urlsafe(5).replace("-", "").replace("_", "")[:8]
    rooms[code] = {
        "owner": callback.from_user.id,
        "players": {
            callback.from_user.id: {
                "name": callback.from_user.full_name[:100],
                "score": 0,
                "private_started": callback.message.chat.type == "private",
            }
        },
        "chat_id": callback.message.chat.id,
        "chat_type": callback.message.chat.type,
        "game": None,
        "selected_game": None,
        "questions": [],
        "question_index": 0,
        "answered": set(),
        "finished": False,
        "question_message_ids": set(),
    }

    me = await callback.bot.get_me()
    invite = f"https://t.me/{me.username}?start=game_{code}"
    await callback.answer("Xona yaratildi!")
    await callback.message.answer(
        "🎮 <b>O‘YIN XONASI TAYYOR!</b>\n\n"
        f"🔑 Xona kodi: <code>{code}</code>\n"
        f"👤 Tashkilotchi: {callback.from_user.full_name}\n"
        f"👥 Ishtirokchilar: 1/30\n\n"
        f"🔗 <a href=\"{invite}\">Do‘stni taklif qilish</a>\n"
        "Guruhda o‘ynash uchun shu xabarni guruhga ulashing yoki "
        "guruhda /games buyrug‘i bilan xona yarating.\n\n"
        "Do‘stlaring qo‘shilgach, o‘yinni boshlang.",
        reply_markup=room_keyboard(code, True),
        disable_web_page_preview=True
    )


@game_router.message(Command("join"))
async def join_command(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Ishlatish: /join XONA_KODI")
        return
    ok, result = await add_player(message.bot, parts[1].strip(), message.from_user,
                                  message.chat.type == "private")
    await message.answer(result)


@game_router.callback_query(F.data.startswith("game_join:"))
async def join_callback(callback: CallbackQuery):
    code = callback.data.split(":", 1)[1]
    ok, result = await add_player(
        callback.bot, code, callback.from_user,
        callback.message.chat.type == "private" if callback.message else False
    )
    await callback.answer(result[:180], show_alert=not ok)
    if ok and callback.message:
        await callback.message.answer(result)


@game_router.callback_query(F.data == "game_top")
async def show_top(callback: CallbackQuery):
    rows = get_top()
    if not rows:
        result = "🏆 Hali reyting mavjud emas. O‘yin o‘ynab ball to‘pla!"
    else:
        result = "🏆 <b>DO‘STLAR BATTLE — TOP 10</b>\n\n"
        medals = ["🥇", "🥈", "🥉"]
        for i, (name, points, played) in enumerate(rows, 1):
            medal = medals[i - 1] if i <= 3 else f"{i}."
            result += f"{medal} {name} — <b>{points}</b> ball · {played} o‘yin\n"
    await callback.answer()
    if callback.message:
        await callback.message.answer(result)


@game_router.callback_query(F.data.startswith("game_start:"))
async def start_game_menu(callback: CallbackQuery):
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
        buttons = [
            [InlineKeyboardButton(text=label, callback_data=f"play:{code}:{key}")]
            for key, label in GAMES.items()
        ]
        await callback.message.answer(
            "🎮 <b>Qaysi o‘yinni boshlaymiz?</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )


@game_router.callback_query(F.data.startswith("play:"))
async def play_game(callback: CallbackQuery):
    try:
        _, code, key = callback.data.split(":")
    except ValueError:
        await callback.answer("Noto‘g‘ri so‘rov.", show_alert=True)
        return

    room = rooms.get(code)
    if not room or key not in QUESTIONS:
        await callback.answer("Xona yoki o‘yin topilmadi.", show_alert=True)
        return
    if callback.from_user.id != room["owner"]:
        await callback.answer("Faqat xona egasi tanlaydi.", show_alert=True)
        return
    if room["game"] is not None:
        await callback.answer("O‘yin allaqachon boshlangan.", show_alert=True)
        return

    room["game"] = key
    room["selected_game"] = key
    room["questions"] = random.sample(QUESTIONS[key], min(5, len(QUESTIONS[key])))
    room["question_index"] = 0
    room["answered"] = set()
    for player in room["players"].values():
        player["score"] = 0

    await callback.answer("O‘yin boshlandi!")
    await callback.bot.send_message(
        room["chat_id"],
        f"🚀 <b>{GAMES[key]} boshlandi!</b>\n"
        f"👥 Ishtirokchilar: {len(room['players'])}"
    )
    await send_question(callback.bot, code)


async def send_question(bot, code: str):
    room = rooms.get(code)
    if not room:
        return
    index = room["question_index"]
    if index >= len(room["questions"]):
        await finish_game(bot, code)
        return

    question, options, _correct = room["questions"][index]
    room["answered"] = set()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=option, callback_data=f"answer:{code}:{i}")]
        for i, option in enumerate(options)
    ])
    text = (
        f"🎮 <b>{GAMES[room['game']]}</b>\n"
        f"📝 Savol {index + 1}/{len(room['questions'])}\n\n"
        f"<b>{question}</b>\n\n"
        "Javobni tanlang:"
    )

    # Guruhdagi o‘yinchilar shu xabarda javob beradi.
    try:
        sent = await bot.send_message(room["chat_id"], text, reply_markup=keyboard)
        room["question_message_ids"].add(sent.message_id)
    except Exception:
        pass

    # Shaxsiy taklif orqali kirganlar ham o‘z DM'larida javob bera oladi.
    for user_id, player in list(room["players"].items()):
        if player["private_started"] and user_id != room["chat_id"]:
            try:
                await bot.send_message(user_id, text, reply_markup=keyboard)
            except Exception:
                # DM yopiq bo‘lsa, guruhdagi savol orqali javob berishi mumkin.
                pass


@game_router.callback_query(F.data.startswith("answer:"))
async def answer_question(callback: CallbackQuery):
    try:
        _, code, answer_raw = callback.data.split(":")
        answer = int(answer_raw)
    except (ValueError, AttributeError):
        await callback.answer("Noto‘g‘ri javob tugmasi.", show_alert=True)
        return

    room = rooms.get(code)
    if not room or room["finished"] or room["game"] is None:
        await callback.answer("O‘yin tugagan.", show_alert=True)
        return

    user_id = callback.from_user.id
    if user_id not in room["players"]:
        await callback.answer("Siz bu o‘yin ishtirokchisi emassiz.", show_alert=True)
        return
    if user_id in room["answered"]:
        await callback.answer("Javobingiz oldin qabul qilingan.", show_alert=True)
        return

    index = room["question_index"]
    question, options, correct = room["questions"][index]
    if answer < 0 or answer >= len(options):
        await callback.answer("Noto‘g‘ri javob.", show_alert=True)
        return

    room["answered"].add(user_id)
    if answer == correct:
        room["players"][user_id]["score"] += 10
        await callback.answer("✅ To‘g‘ri! +10 ball")
    else:
        await callback.answer("❌ Noto‘g‘ri!")

    # Hamma javob bergach keyingi savolga o'tadi.
    if len(room["answered"]) >= len(room["players"]):
        room["question_index"] += 1
        await send_question(callback.bot, code)


async def finish_game(bot, code: str):
    room = rooms.get(code)
    if not room or room["finished"]:
        return
    room["finished"] = True

    ranking = sorted(
        room["players"].items(),
        key=lambda item: item[1]["score"],
        reverse=True
    )
    lines = ["🏆 <b>O‘YIN YAKUNLANDI!</b>\n"]
    for i, (user_id, player) in enumerate(ranking, 1):
        points = player["score"]
        medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
        lines.append(f"{medal} {player['name']} — <b>{points}</b> ball")
        add_points(user_id, player["name"], points)

    lines.append("\n🎮 Yana o‘ynash uchun /games buyrug‘ini yuboring.")
    try:
        await bot.send_message(room["chat_id"], "\n".join(lines))
    except Exception:
        pass

    # Xona yakunlanganidan keyin xotiradan chiqariladi.
    rooms.pop(code, None)
