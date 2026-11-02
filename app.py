
import os
import re
import sqlite3
import logging
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
import telebot
import yt_dlp
from flask import Flask, request
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

# =========================
# SOZLAMALAR
# =========================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL = os.getenv("CHANNEL_USERNAME", "@akoshsaved")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi!")

bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)

DB = "akoshsaved.db"
MAX_SIZE = 49 * 1024 * 1024

ALLOWED_DOMAINS = {
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com",
    "vt.tiktok.com",
    "instagram.com", "www.instagram.com",
    "youtube.com", "www.youtube.com",
    "m.youtube.com", "youtu.be"
}

# Foydalanuvchi holatlari
user_states = {}

# Qidiruv natijalari
search_results = {}


# =========================
# DATABASE
# =========================

def db():
    return sqlite3.connect(DB)


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS saved_songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                artist TEXT,
                url TEXT,
                UNIQUE(user_id, title, artist)
            )
        """)


init_db()


# =========================
# MENYU
# =========================

def main_menu():
    markup = InlineKeyboardMarkup(row_width=2)

    markup.add(
        InlineKeyboardButton("📥 Video yuklash", callback_data="video"),
        InlineKeyboardButton("🎵 Musiqa qidirish", callback_data="music")
    )

    markup.add(
        InlineKeyboardButton("⭐ Saqlanganlar", callback_data="saved"),
        InlineKeyboardButton("ℹ️ Yordam", callback_data="help")
    )

    return markup


def subscription_menu():
    markup = InlineKeyboardMarkup()

    markup.add(
        InlineKeyboardButton(
            "📢 Kanalga obuna bo‘lish",
            url="https://t.me/akoshsaved"
        )
    )

    markup.add(
        InlineKeyboardButton(
            "✅ Obunani tekshirish",
            callback_data="check_sub"
        )
    )

    return markup


# =========================
# OBUNANI TEKSHIRISH
# =========================

def is_subscribed(user_id):
    try:
        member = bot.get_chat_member(CHANNEL, user_id)

        return member.status in (
            "member", "administrator", "creator"
        )

    except Exception:
        logger.exception("Obuna tekshirishda xato")
        return False


def require_subscription(message):
    if is_subscribed(message.from_user.id):
        return True

    bot.send_message(
        message.chat.id,
        "⛔ Botdan foydalanish uchun kanalga obuna bo‘ling.",
        reply_markup=subscription_menu()
    )

    return False


# =========================
# START
# =========================

@bot.message_handler(commands=["start"])
def start(message):
    user_states.pop(message.from_user.id, None)

    if not require_subscription(message):
        return

    bot.send_message(
        message.chat.id,
        "👋 AKOSHSAVEDBOT ga xush kelibsiz!\n\n"
        "📥 TikTok, Instagram va YouTube havolasini yuboring.\n"
        "🎵 Qo‘shiq nomi yoki ijrochi ismini qidirishingiz mumkin.\n\n"
        "Kerakli bo‘limni tanlang:",
        reply_markup=main_menu()
    )


# =========================
# OBUNA CALLBACK
# =========================

@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def check_sub_callback(call):
    if is_subscribed(call.from_user.id):
        bot.answer_callback_query(
            call.id, "Obuna tasdiqlandi!"
        )

        bot.send_message(
            call.message.chat.id,
            "✅ Obuna tasdiqlandi. Botdan foydalanishingiz mumkin!",
            reply_markup=main_menu()
        )
    else:
        bot.answer_callback_query(
            call.id,
            "Kanalga obuna bo‘ling va qayta tekshiring.",
            show_alert=True
        )


# =========================
# MUSIQA QIDIRISH
# =========================

def search_music(query):
    response = requests.get(
        "https://itunes.apple.com/search",
        params={
            "term": query,
            "entity": "song",
            "limit": 8,
            "country": "US"
        },
        timeout=15
    )

    response.raise_for_status()
    return response.json().get("results", [])


def music_results(message, query):
    try:
        results = search_music(query)

        if not results:
            bot.send_message(
                message.chat.id,
                "😕 Bu nom bo‘yicha qo‘shiq topilmadi."
            )
            return

        for index, song in enumerate(results[:8]):
            title = song.get("trackName", "Noma'lum")
            artist = song.get("artistName", "Noma'lum")
            album = song.get("collectionName", "Noma'lum")
            link = song.get("trackViewUrl", "")

            search_id = f"{message.from_user.id}_{index}"
            search_results[search_id] = {
                "title": title,
                "artist": artist,
                "url": link
            }

            markup = InlineKeyboardMarkup()

            if link:
                markup.add(
                    InlineKeyboardButton(
                        "▶️ Tinglash sahifasini ochish",
                        url=link
                    )
                )

            markup.add(
                InlineKeyboardButton(
                    "⭐ Saqlash",
                    callback_data=f"save:{index}"
                )
            )

            bot.send_message(
                message.chat.id,
                f"🎵 {title}\n"
                f"👤 Ijrochi: {artist}\n"
                f"💿 Albom: {album}",
                reply_markup=markup
            )

        # Oxirgi qidiruvni foydalanuvchi bo‘yicha saqlash
        user_states[message.from_user.id] = {
            "type": "music_results",
            "items": results[:8]
        }

    except Exception:
        logger.exception("Musiqa qidirish xatosi")
        bot.send_message(
            message.chat.id,
            "Musiqa qidirishda xatolik yuz berdi. Keyinroq urinib ko‘ring."
        )


@bot.callback_query_handler(func=lambda c: c.data.startswith("save:"))
def save_song(call):
    state = user_states.get(call.from_user.id)

    if not state or state.get("type") != "music_results":
        bot.answer_callback_query(
            call.id, "Qidiruv natijasi eskirgan. Qayta qidiring."
        )
        return

    try:
        index = int(call.data.split(":")[1])
        song = state["items"][index]

        title = song.get("trackName", "Noma'lum")
        artist = song.get("artistName", "Noma'lum")
        url = song.get("trackViewUrl", "")

        with db() as con:
            con.execute(
                """
                INSERT OR IGNORE INTO saved_songs
                (user_id, title, artist, url)
                VALUES (?, ?, ?, ?)
                """,
                (call.from_user.id, title, artist, url)
            )

        bot.answer_callback_query(call.id, "⭐ Saqlandi!")

    except Exception:
        logger.exception("Saqlash xatosi")
        bot.answer_callback_query(
            call.id, "Saqlashda xatolik yuz berdi."
        )


# =========================
# SAQLANGAN QO‘SHIQLAR
# =========================

@bot.message_handler(commands=["saved"])
def saved_command(message):
    show_saved(message.chat.id, message.from_user.id)


def show_saved(chat_id, user_id):
    with db() as con:
        songs = con.execute(
            """
            SELECT title, artist, url
            FROM saved_songs
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 30
            """,
            (user_id,)
        ).fetchall()

    if not songs:
        bot.send_message(
            chat_id,
            "⭐ Hozircha saqlangan qo‘shiqlaringiz yo‘q."
        )
        return

    bot.send_message(chat_id, "⭐ Saqlangan qo‘shiqlaringiz:")

    for title, artist, url in songs:
        markup = InlineKeyboardMarkup()

        if url:
            markup.add(
                InlineKeyboardButton(
                    "▶️ Tinglash sahifasi",
                    url=url
                )
            )

        bot.send_message(
            chat_id,
            f"🎵 {title}\n👤 {artist}",
            reply_markup=markup
        )


# =========================
# VIDEO HAVOLASI
# =========================

def valid_url(url):
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":")[0]

        return (
            parsed.scheme in ("http", "https")
            and host in ALLOWED_DOMAINS
        )
    except Exception:
        return False


def download_video(message, url):
    status = bot.send_message(
        message.chat.id,
        "⏳ Video tekshirilmoqda..."
    )

    try:
        with tempfile.TemporaryDirectory() as folder:
            output = str(Path(folder) / "video.%(ext)s")

            options = {
                "outtmpl": output,
                "format": "best[filesize<49M]/best",
                "max_filesize": MAX_SIZE,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 20,
                "retries": 1
            }

            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([url])

            files = list(Path(folder).glob("video.*"))

            if not files:
                raise Exception("Video topilmadi.")

            file_path = files[0]

            if file_path.stat().st_size > MAX_SIZE:
                raise Exception("Video hajmi juda katta.")

            with open(file_path, "rb") as video:
                if file_path.suffix.lower() in (
                    ".mp4", ".mov", ".m4v"
                ):
                    bot.send_video(
                        message.chat.id,
                        video,
                        caption="✅ AKOSHSAVEDBOT"
                    )
                else:
                    bot.send_document(
                        message.chat.id,
                        video,
                        caption="✅ AKOSHSAVEDBOT"
                    )

            bot.delete_message(
                message.chat.id,
                status.message_id
            )

    except Exception:
        logger.exception("Video yuklashda xato")

        bot.edit_message_text(
            "❌ Videoni yuklab bo‘lmadi.\n"
            "Havolani tekshiring yoki keyinroq urinib ko‘ring.\n"
            "Ayrim platformalar videoni yuklashga ruxsat bermasligi mumkin.",
            message.chat.id,
            status.message_id
        )


# =========================
# MENYU CALLBACKLARI
# =========================

@bot.callback_query_handler(
    func=lambda c: c.data in ("video", "music", "saved", "help")
)
def menu_callback(call):
    if not is_subscribed(call.from_user.id):
        bot.answer_callback_query(
            call.id, "Avval kanalga obuna bo‘ling.", show_alert=True
        )
        bot.send_message(
            call.message.chat.id,
            "📢 Kanalga obuna bo‘ling:",
            reply_markup=subscription_menu()
        )
        return

    bot.answer_callback_query(call.id)

    if call.data == "video":
        user_states[call.from_user.id] = {"type": "video"}
        bot.send_message(
            call.message.chat.id,
            "📥 TikTok, Instagram yoki YouTube havolasini yuboring."
        )

    elif call.data == "music":
        user_states[call.from_user.id] = {"type": "music"}
        bot.send_message(
            call.message.chat.id,
            "🎵 Qo‘shiq nomi yoki ijrochi ismini yozing."
        )

    elif call.data == "saved":
        show_saved(
            call.message.chat.id,
            call.from_user.id
        )

    elif call.data == "help":
        bot.send_message(
            call.message.chat.id,
            "ℹ️ Yordam:\n\n"
            "📥 Video — video havolasini yuboring.\n"
            "🎵 Musiqa — qo‘shiq yoki ijrochi nomini yozing.\n"
            "⭐ /saved — saqlangan qo‘shiqlar."
        )


# =========================
# MATN XABARLARI
# =========================

@bot.message_handler(content_types=["text"])
def text_handler(message):
    user_id = message.from_user.id
    text = message.text.strip()

    if text.startswith("/"):
        return

    if not require_subscription(message):
        return

    state = user_states.get(user_id, {})

    if state.get("type") == "music":
        user_states.pop(user_id, None)
        music_results(message, text)
        return

    if valid_url(text):
        user_states.pop(user_id, None)
        download_video(message, text)
        return

    bot.send_message(
        message.chat.id,
        "Kerakli bo‘limni tanlang yoki video havolasini yuboring.",
        reply_markup=main_menu()
    )


# =========================
# WEBHOOK
# =========================

@app.route("/", methods=["GET"])
def home():
    return "AKOSHSAVEDBOT IS RUNNING", 200


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True)

        if not data:
            return "No update", 400

        update = telebot.types.Update.de_json(data)
        bot.process_new_updates([update])

        return "OK", 200

    except Exception:
        logger.exception("Webhook xatosi")
        return "Webhook error", 500


def connect_webhook():
    if not RENDER_URL:
        logger.error("RENDER_EXTERNAL_URL topilmadi")
        return

    try:
        url = RENDER_URL.rstrip("/") + "/webhook"
        bot.remove_webhook()
        result = bot.set_webhook(url=url)

        if result:
            logger.info("AKOSHSAVEDBOT WEBHOOK CONNECTED")
        else:
            logger.error("Webhook ulanmadi")

    except Exception:
        logger.exception("Webhook ulashda xato")


connect_webhook()
