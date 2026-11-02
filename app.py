
import os
import logging
from flask import Flask, request
import telebot

# LOG
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ENVIRONMENT VARIABLES
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN Render Environment Variables ichida yo'q!")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID Render Environment Variables ichida yo'q!")

ADMIN_ID = int(ADMIN_ID)

# BOT
bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)


# START COMMAND
@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(
        message,
        "Assalomu alaykum!\n\n"
        "📱 TELEFON NARXLASH botiga xush kelibsiz.\n\n"
        "Telefoningiz modelini yuboring.\n"
        "Masalan: iPhone 13 Pro 128GB"
    )


# HELP COMMAND
@bot.message_handler(commands=["help"])
def help_command(message):
    bot.reply_to(
        message,
        "Telefon modelini va holatini yozib yuboring.\n"
        "Savollar uchun administratorga murojaat qiling."
    )


# PHONE INFORMATION
@bot.message_handler(content_types=["text"])
def phone_info(message):
    if message.text.startswith("/"):
        return

    try:
        bot.send_message(
            ADMIN_ID,
            "📱 YANGI TELEFON SO'ROVI\n\n"
            f"👤 Ism: {message.from_user.first_name}\n"
            f"🆔 Telegram ID: {message.from_user.id}\n"
            f"📝 Ma'lumot: {message.text}"
        )

        bot.reply_to(
            message,
            "✅ Ma'lumotingiz adminga yuborildi.\n"
            "Javobni kuting."
        )

    except Exception as error:
        logger.exception("Xabar yuborishda xato: %s", error)
        bot.reply_to(message, "Xatolik yuz berdi. Keyinroq urinib ko'ring.")


# PHONE PHOTO
@bot.message_handler(content_types=["photo"])
def phone_photo(message):
    try:
        bot.send_message(
            ADMIN_ID,
            "📸 YANGI TELEFON RASMI\n"
            f"👤 Foydalanuvchi: {message.from_user.first_name}\n"
            f"🆔 ID: {message.from_user.id}"
        )

        bot.forward_message(
            ADMIN_ID,
            message.chat.id,
            message.message_id
        )

        bot.reply_to(message, "✅ Rasm adminga yuborildi.")

    except Exception as error:
        logger.exception("Rasm yuborishda xato: %s", error)
        bot.reply_to(message, "Rasm yuborilmadi. Qayta urinib ko'ring.")


# HEALTH CHECK
@app.route("/", methods=["GET"])
def home():
    return "TELEFON NARXLASH BOT IS RUNNING", 200


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


# TELEGRAM WEBHOOK
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        update_json = request.get_json(silent=True)

        if not update_json:
            return "No update", 400

        update = telebot.types.Update.de_json(update_json)
        bot.process_new_updates([update])

        return "OK", 200

    except Exception as error:
        logger.exception("Webhook xatosi: %s", error)
        return "Webhook error", 500


# CONNECT WEBHOOK
def connect_webhook():
    render_url = os.environ.get("RENDER_EXTERNAL_URL")

    if not render_url:
        logger.error("RENDER_EXTERNAL_URL TOPILMADI")
        return

    webhook_url = render_url.rstrip("/") + "/webhook"

    try:
        bot.remove_webhook()
        result = bot.set_webhook(url=webhook_url)

        if result:
            logger.info("TELEGRAM WEBHOOK CONNECTED: %s", webhook_url)
        else:
            logger.error("WEBHOOK SET FAILED")

    except Exception as error:
        logger.exception("WEBHOOK ERROR: %s", error)


connect_webhook()
