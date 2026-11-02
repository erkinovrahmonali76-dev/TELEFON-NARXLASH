
import os
import re
import logging
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, request
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp

# LOG
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# SOZLAMALAR
TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL = os.environ.get("CHANNEL_USERNAME", "@akoshsaved")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN Render Environment Variables ichida yo'q!")

bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)

MAX_SIZE = 49 * 1024 * 1024

ALLOWED_DOMAINS = {
    "tiktok.com",
    "www.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "instagram.com",
    "www.instagram.com",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "m.youtube.com",
}


def main_menu():
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🎵 Musiqa", callback_data="music"),
        InlineKeyboardButton("⭐ Saqlanganlar", callback_data="saved")
    )
    markup.row(
        InlineKeyboardButton("🌐 Til", callback_data="language"),
        InlineKeyboardButton("ℹ️ Yordam", callback_data="help")
    )
    return markup


def join_menu():
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "📢 Kanalga obuna bo'lish",
            url="https://t.me/" + CHANNEL.replace("@", "")
        )
    )
    markup.add(
        InlineKeyboardButton(
            "✅ Obunani tekshirish",
            callback_data="check_sub"
        )
    )
    return markup


def is_subscribed(user_id):
    try:
        member = bot.get_chat_member(CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as error:
        logger.error("Obuna tekshirish xatosi: %s", error)
        return False


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


@bot.message_handler(commands=["start"])
def start(message):
    if not is_subscribed(message.from_user.id):
        bot.send_message(
            message.chat.id,
            "👋 AKOSHSAVEDBOT ga xush kelibsiz!\n\n"
            "Davom etish uchun kanalimizga obuna bo'ling.",
            reply_markup=join_menu()
        )
        return

    bot.send_message(
        message.chat.id,
        "👋 Salom! AKOSHSAVEDBOT ga xush kelibsiz!\n\n"
        "📥 TikTok, Instagram yoki YouTube havolasini yuboring.\n\n"
        "Faqat o'zingizga tegishli yoki yuklashga ruxsat berilgan "
        "videolardan foydalaning.",
        reply_markup=main_menu()
    )


@bot.message_handler(commands=["saved"])
def saved(message):
    bot.reply_to(
        message,
        "⭐ Saqlanganlar funksiyasi keyingi versiyada qo'shiladi."
    )


@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    if call.data == "check_sub":
        if is_subscribed(call.from_user.id):
            bot.answer_callback_query(
                call.id, "Obuna tasdiqlandi!"
            )
            bot.send_message(
                call.message.chat.id,
                "✅ Xush kelibsiz! Endi video havolasini yuboring.",
                reply_markup=main_menu()
            )
        else:
            bot.answer_callback_query(
                call.id,
                "Avval kanalga obuna bo'ling.",
                show_alert=True
            )

    elif call.data == "saved":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "⭐ Saqlanganlar keyingi versiyada qo'shiladi."
        )

    elif call.data == "music":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🎵 Musiqa qidirish funksiyasi keyingi versiyada qo'shiladi."
        )

    elif call.data == "language":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🌐 Hozircha bot o'zbek tilida ishlaydi."
        )

    elif call.data == "help":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "ℹ️ TikTok, Instagram yoki YouTube havolasini yuboring."
        )


@bot.message_handler(content_types=["text"])
def download_video(message):
    url = message.text.strip()

    if not valid_url(url):
        bot.reply_to(
            message,
            "❌ Havola noto'g'ri.\n"
            "TikTok, Instagram yoki YouTube havolasini yuboring."
        )
        return

    if not is_subscribed(message.from_user.id):
        bot.send_message(
            message.chat.id,
            "Davom etish uchun kanalga obuna bo'ling.",
            reply_markup=join_menu()
        )
        return

    status = bot.reply_to(message, "⏳ Havola tekshirilmoqda...")

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_template = str(Path(temp_dir) / "video.%(ext)s")

            options = {
                "outtmpl": output_template,
                "format": "best[filesize<49M]/best",
                "max_filesize": MAX_SIZE,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 20,
                "retries": 1,
            }

            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)

            files = list(Path(temp_dir).glob("video.*"))

            if not files:
                raise Exception("Video fayli topilmadi.")

            file_path = files[0]

            if file_path.stat().st_size > MAX_SIZE:
                raise Exception("Video hajmi 49 MB dan katta.")

            bot.edit_message_text(
                "📤 Video Telegram'ga yuborilmoqda...",
                message.chat.id,
                status.message_id
            )

            with open(file_path, "rb") as video:
                if file_path.suffix.lower() in (
                    ".mp4", ".mov", ".m4v"
                ):
                    bot.send_video(
                        message.chat.id,
                        video,
                        caption="✅ AKOSHSAVEDBOT orqali tayyorlandi."
                    )
                else:
                    bot.send_document(
                        message.chat.id,
                        video,
                        caption="✅ AKOSHSAVEDBOT orqali tayyorlandi."
                    )

            bot.delete_message(
                message.chat.id,
                status.message_id
            )

    except Exception as error:
        logger.exception("Yuklash xatosi: %s", error)
        bot.edit_message_text(
            "❌ Videoni yuklab bo'lmadi.\n\n"
            "Havolani tekshiring yoki keyinroq urinib ko'ring. "
            "Ayrim platformalar yoki videolar qo'llab-quvvatlanmasligi mumkin.",
            message.chat.id,
            status.message_id
        )


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
        webhook_url = RENDER_URL.rstrip("/") + "/webhook"
        bot.remove_webhook()
        result = bot.set_webhook(url=webhook_url)

        if result:
            logger.info("AKOSHSAVEDBOT WEBHOOK CONNECTED")
        else:
            logger.error("Webhook ulanmadi")

    except Exception:
        logger.exception("Webhook ulashda xato")


connect_webhook()
