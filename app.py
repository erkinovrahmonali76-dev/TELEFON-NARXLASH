
import os
import sqlite3
import telebot
from telebot import types
from flask import Flask, request

# SETTINGS
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
PRICE = 10000

if not TOKEN:
    raise RuntimeError("BOT_TOKEN sozlanmagan")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# DATABASE
def db():
    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    return conn

with db() as conn:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        brand TEXT,
        model TEXT,
        memory TEXT,
        condition TEXT,
        method TEXT,
        status TEXT DEFAULT 'new',
        ai_price TEXT,
        admin_price TEXT
    )
    """)

users = {}

def keyboard(rows):
    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True
    )
    for row in rows:
        markup.row(*row)
    return markup

def lang_text(lang, uz, ru, en):
    return {"uz": uz, "ru": ru, "en": en}.get(lang, uz)

@bot.message_handler(commands=["start"])
def start(message):
    markup = keyboard([["🇺🇿 O‘zbekcha", "🇷🇺 Русский", "🇬🇧 English"]])
    bot.send_message(
        message.chat.id,
        "TELEFON NARXLASH\nTilni tanlang / Выберите язык / Choose language",
        reply_markup=markup
    )

@bot.message_handler(func=lambda m: m.text in [
    "🇺🇿 O‘zbekcha", "🇷🇺 Русский", "🇬🇧 English"
])
def choose_language(message):
    lang = {
        "🇺🇿 O‘zbekcha": "uz",
        "🇷🇺 Русский": "ru",
        "🇬🇧 English": "en"
    }[message.text]

    users[message.chat.id] = {"lang": lang}

    bot.send_message(
        message.chat.id,
        lang_text(lang, "📱 Telefon turini tanlang:",
                  "📱 Выберите марку телефона:",
                  "📱 Choose phone brand:"),
        reply_markup=keyboard([
            ["Apple iPhone", "Samsung"],
            ["Xiaomi", "Redmi", "POCO"],
            ["Huawei", "Honor", "Vivo"],
            ["Oppo", "Realme", "Boshqa"]
        ])
    )

@bot.message_handler(func=lambda m: m.text in [
    "Apple iPhone", "Samsung", "Xiaomi", "Redmi",
    "POCO", "Huawei", "Honor", "Vivo",
    "Oppo", "Realme", "Boshqa"
])
def choose_brand(message):
    data = users.setdefault(message.chat.id, {})
    data["brand"] = message.text
    msg = bot.send_message(
        message.chat.id,
        "Telefon modelini yozing (masalan: iPhone 13 Pro Max):"
    )
    bot.register_next_step_handler(msg, choose_model)

def choose_model(message):
    data = users.setdefault(message.chat.id, {})
    data["model"] = message.text
    msg = bot.send_message(message.chat.id, "Xotirasini yozing (128GB, 256GB...):")
    bot.register_next_step_handler(msg, choose_memory)

def choose_memory(message):
    users[message.chat.id]["memory"] = message.text
    msg = bot.send_message(message.chat.id, "Telefon holatini yozing:")
    bot.register_next_step_handler(msg, choose_condition)

def choose_condition(message):
    users[message.chat.id]["condition"] = message.text
    bot.send_message(
        message.chat.id,
        "Narxlash turini tanlang:",
        reply_markup=keyboard([
            ["🤖 Faqat AI"],
            ["👨‍💼 Faqat Admin"],
            ["🤖 AI + 👨‍💼 Admin"]
        ])
    )

@bot.message_handler(func=lambda m: m.text in [
    "🤖 Faqat AI", "👨‍💼 Faqat Admin",
    "🤖 AI + 👨‍💼 Admin"
])
def choose_method(message):
    data = users.setdefault(message.chat.id, {})
    data["method"] = message.text

    with db() as conn:
        cur = conn.execute("""
        INSERT INTO orders
        (user_id, brand, model, memory, condition, method)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (
            message.chat.id, data["brand"], data["model"],
            data["memory"], data["condition"], data["method"]
        ))
        order_id = cur.lastrowid

    data["order_id"] = order_id

    bot.send_message(
        message.chat.id,
        f"Buyurtma №{order_id}\n"
        f"Xizmat narxi: {PRICE:,} so‘m\n\n"
        "To‘lovni amalga oshiring va chek rasmini yuboring.\n"
        "To‘lov rekvizitlari administrator tomonidan beriladi."
    )

@bot.message_handler(content_types=["photo"])
def receipt(message):
    data = users.get(message.chat.id)
    if not data or not data.get("order_id"):
        bot.reply_to(message, "Avval buyurtma yarating.")
        return

    order_id = data["order_id"]
    caption = (
        f"Yangi to‘lov cheki\n"
        f"Buyurtma: #{order_id}\n"
        f"Foydalanuvchi: {message.from_user.id}\n"
        f"Model: {data['brand']} {data['model']}\n"
        f"Usul: {data['method']}"
    )

    bot.send_photo(
        ADMIN_ID,
        message.photo[-1].file_id,
        caption=caption,
        reply_markup=keyboard([
            [f"approve_{order_id}", f"reject_{order_id}"]
        ])
    )
    bot.reply_to(message, "Chek adminga yuborildi. Tasdiqni kuting.")

@bot.message_handler(func=lambda m: m.text and m.text.startswith("approve_"))
def approve(message):
    if message.from_user.id != ADMIN_ID:
        return
    order_id = int(message.text.split("_")[1])
    with db() as conn:
        conn.execute(
            "UPDATE orders SET status='paid' WHERE id=?",
            (order_id,)
        )
    bot.send_message(
        message.chat.id,
        f"Buyurtma #{order_id} tasdiqlandi."
    )

@bot.message_handler(commands=["admin"])
def admin(message):
    if message.from_user.id != ADMIN_ID:
        return
    with db() as conn:
        orders = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT 20"
        ).fetchall()

    for order in orders:
        bot.send_message(
            message.chat.id,
            f"#{order['id']} | {order['brand']} {order['model']} "
            f"| {order['status']} | {order['method']}"
        )

@app.route("/", methods=["GET"])
def home():
    return "TELEFON NARXLASH BOT IS RUNNING"

@app.route("/webhook", methods=["POST"])
def webhook():
    update = telebot.types.Update.de_json(
        request.get_data().decode("utf-8")
    )
    bot.process_new_updates([update])
    return "OK", 200

