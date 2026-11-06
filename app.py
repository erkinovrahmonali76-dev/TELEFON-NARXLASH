import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, FSInputFile
)
from aiogram.webhook.aiohttp_server import (
    SimpleRequestHandler, setup_application
)
from livekit import api
import yt_dlp


# ==================================================
# SOZLAMALAR
# ==================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook").strip("/")

# FAQAT SHU KANAL
CHANNEL_USERNAME = "@ReklamaBozoriUzz"
CHANNEL_URL = "https://t.me/ReklamaBozoriUzz"

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "").strip()
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "").strip()
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "").strip()

MINI_APP_URL = os.getenv(
    "MINI_APP_URL",
    f"{BASE_URL}/chat"
).strip()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_MODEL_ID = os.getenv(
    "ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"
)

MALE_VOICE_ID = os.getenv(
    "MALE_VOICE_ID", "pNInz6obpgDQGcFmaJgB"
)
FEMALE_VOICE_ID = os.getenv(
    "FEMALE_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"
)

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv(
        "ADMIN_IDS", os.getenv("ADMIN_ID", "")
    ).split(",")
    if x.strip().isdigit()
}

DB_PATH = "asaved_users.db"

MAX_VIDEO_MB = 20
MAX_OUTPUT_MB = 49
MAX_DURATION = 600

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ASAVED")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN sozlanmagan.")

if not BASE_URL.startswith("https://"):
    raise RuntimeError("RENDER_EXTERNAL_URL HTTPS bo‘lishi kerak.")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

video_modes = {}
link_pending = set()
tts_pending = set()
tts_text = {}

bot_username = ""


# ==================================================
# DATABASE
# ==================================================

def connect_db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = connect_db()

    con.executescript("""
    CREATE TABLE IF NOT EXISTS bot_users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS chat_sessions (
        user_id INTEGER PRIMARY KEY,
        status TEXT NOT NULL,
        room_name TEXT,
        updated_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS chat_invites (
        code TEXT PRIMARY KEY,
        room_name TEXT NOT NULL,
        owner_id INTEGER NOT NULL,
        joined_by INTEGER,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS chat_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reporter_id INTEGER,
        reported_id INTEGER,
        room_name TEXT,
        reason TEXT,
        created_at INTEGER
    );
    """)

    con.commit()
    con.close()


def save_user(user):
    con = connect_db()
    con.execute("""
        INSERT INTO bot_users
        (user_id, username, first_name, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
        username=excluded.username,
        first_name=excluded.first_name
    """, (
        user.id,
        user.username or "",
        user.first_name or "",
        int(time.time())
    ))
    con.commit()
    con.close()


# ==================================================
# OBUNA TEKSHIRISH
# ==================================================

def subscribed_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📢 Kanalga obuna bo‘lish",
                url=CHANNEL_URL
            )
        ],
        [
            InlineKeyboardButton(
                text="✅ Obunani tekshirish",
                callback_data="check_subscription"
            )
        ]
    ])


async def is_subscribed(user_id):
    try:
        member = await bot.get_chat_member(
            CHANNEL_USERNAME,
            user_id
        )

        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR
        )

    except Exception as e:
        logger.warning("Obuna tekshirish xatosi: %s", e)
        return False


async def ask_sub(message):
    await message.answer(
        "Botdan foydalanish uchun quyidagi kanalga obuna bo‘ling:",
        reply_markup=subscribed_button()
    )


# ==================================================
# KEYBOARDLAR
# ==================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🎥 Dumaloq video"),
                KeyboardButton(text="📱 9:16 format")
            ],
            [
                KeyboardButton(text="🔗 Video havolasi"),
                KeyboardButton(text="🎙️ Matnni ovozga aylantirish")
            ],
            [
                KeyboardButton(
                    text="🌐 ASAVED CHAT",
                    web_app=WebAppInfo(url=MINI_APP_URL)
                )
            ]
        ],
        resize_keyboard=True
    )


def tts_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="👨 Erkak ovozi",
                callback_data="tts:male"
            ),
            InlineKeyboardButton(
                text="👩 Ayol ovozi",
                callback_data="tts:female"
            )
        ]
    ])


# ==================================================
# START
# ==================================================

@dp.message(CommandStart())
async def start(message: Message):
    save_user(message.from_user)

    if not await is_subscribed(message.from_user.id):
        await ask_sub(message)
        return

    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""

    if payload.startswith("invite_"):
        code = payload.replace("invite_", "", 1)

        invite_url = f"{MINI_APP_URL}?invite={code}"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎥 Taklif qilingan suhbatga kirish",
                    web_app=WebAppInfo(url=invite_url)
                )
            ]
        ])

        await message.answer(
            "Sizga shaxsiy video suhbat taklifi yuborildi.",
            reply_markup=kb
        )
        return

    await message.answer(
        "👋 ASAVED botiga xush kelibsiz!\n\n"
        "Kerakli xizmatni tanlang:",
        reply_markup=main_keyboard()
    )


@dp.callback_query(F.data == "check_subscription")
async def check_subscription(call: CallbackQuery):
    if await is_subscribed(call.from_user.id):
        save_user(call.from_user)

        await call.message.answer(
            "✅ Obuna tasdiqlandi!",
            reply_markup=main_keyboard()
        )
        await call.answer("Obuna tasdiqlandi.")

    else:
        await call.answer(
            "Avval @ReklamaBozoriUzz kanaliga obuna bo‘ling.",
            show_alert=True
        )


# ==================================================
# VIDEO MENYUSI
# ==================================================

@dp.message(F.text == "🎥 Dumaloq video")
async def round_start(message: Message):
    if not await is_subscribed(message.from_user.id):
        await ask_sub(message)
        return

    video_modes[message.from_user.id] = "round"
    await message.answer(
        "🎥 Dumaloq videoga aylantirmoqchi bo‘lgan videoni yuboring."
    )


@dp.message(F.text == "📱 9:16 format")
async def vertical_start(message: Message):
    if not await is_subscribed(message.from_user.id):
        await ask_sub(message)
        return

    video_modes[message.from_user.id] = "vertical"
    await message.answer(
        "📱 9:16 formatga aylantirish uchun videoni yuboring."
    )


@dp.message(F.text == "🔗 Video havolasi")
async def link_start(message: Message):
    if not await is_subscribed(message.from_user.id):
        await ask_sub(message)
        return

    link_pending.add(message.from_user.id)
    await message.answer(
        "YouTube, Instagram yoki TikTok video havolasini yuboring."
    )


@dp.message(F.text == "🎙️ Matnni ovozga aylantirish")
async def tts_start(message: Message):
    if not await is_subscribed(message.from_user.id):
        await ask_sub(message)
        return

    tts_pending.add(message.from_user.id)
    await message.answer(
        "Ovozga aylantiriladigan matnni yuboring. "
        "Maksimum 3000 belgi."
    )


# ==================================================
# TELEGRAM VIDEONI YUKLASH
# ==================================================

async def download_telegram_video(message):
    media = message.video

    if not media and message.document:
        if not (message.document.mime_type or "").startswith("video/"):
            return None
        media = message.document

    if not media:
        return None

    if (
        media.file_size
        and media.file_size > MAX_VIDEO_MB * 1024 * 1024
    ):
        await message.answer(
            f"Video {MAX_VIDEO_MB} MB dan kichik bo‘lishi kerak."
        )
        return None

    file = await bot.get_file(media.file_id)

    folder = tempfile.mkdtemp(prefix="asaved_")
    source = os.path.join(folder, "input.mp4")

    await bot.download_file(
        file.file_path,
        destination=source
    )

    return folder, source


async def ffmpeg_convert(source, output, mode):
    if mode == "round":
        vf = (
            "scale=720:720:"
            "force_original_aspect_ratio=increase,"
            "crop=720:720"
        )
    else:
        vf = (
            "scale=720:1280:"
            "force_original_aspect_ratio=increase,"
            "crop=720:1280"
        )

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i", source,
        "-vf", vf,
        "-t", str(MAX_DURATION),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    _, error = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(
            error.decode(errors="ignore")[-1000:]
        )

    if os.path.getsize(output) > MAX_OUTPUT_MB * 1024 * 1024:
        raise RuntimeError("Tayyor video 49 MB dan oshib ketdi.")


@dp.message(F.video | F.document)
async def video_handler(message: Message):
    uid = message.from_user.id
    mode = video_modes.get(uid)

    if not mode:
        return

    if not await is_subscribed(uid):
        await ask_sub(message)
        return

    data = await download_telegram_video(message)

    if not data:
        return

    folder, source = data
    output = os.path.join(folder, "result.mp4")

    status = await message.answer("⏳ Video tayyorlanmoqda...")

    try:
        await ffmpeg_convert(source, output, mode)

        if mode == "round":
            await message.answer_video_note(
                FSInputFile(output)
            )
        else:
            await message.answer_video(
                FSInputFile(output),
                caption="✅ 9:16 format tayyor!"
            )

        video_modes.pop(uid, None)
        await status.delete()

    except Exception as e:
        logger.exception("Video xatosi")
        await status.edit_text(
            f"❌ Xatolik: {str(e)[:400]}"
        )

    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ==================================================
# VIDEO HAVOLASINI YUKLASH
# ==================================================

async def download_link(url, folder):
    def worker():
        options = {
            "outtmpl": os.path.join(
                folder, "download.%(ext)s"
            ),
            "format": "best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "max_filesize": MAX_OUTPUT_MB * 1024 * 1024,
            "quiet": True,
            "no_warnings": True
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

    await asyncio.to_thread(worker)

    files = [
        os.path.join(folder, name)
        for name in os.listdir(folder)
        if name.startswith("download.")
    ]

    if not files:
        raise RuntimeError("Video topilmadi.")

    return files[0]


@dp.message(F.text)
async def text_handler(message: Message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    if uid in tts_pending:
        if not text:
            return

        if len(text) > 3000:
            await message.answer(
                "Matn 3000 belgidan oshmasin."
            )
            return

        tts_pending.discard(uid)
        tts_text[uid] = text

        await message.answer(
            "Qaysi ovoz kerak?",
            reply_markup=tts_keyboard()
        )
        return

    if uid in link_pending:
        link_pending.discard(uid)

        parsed = urlparse(text)

        if parsed.scheme not in ("https", "http"):
            await message.answer("❌ To‘g‘ri havola yuboring.")
            return

        host = (parsed.hostname or "").lower()

        allowed = (
            host == "youtube.com"
            or host.endswith(".youtube.com")
            or host == "youtu.be"
            or host == "instagram.com"
            or host.endswith(".instagram.com")
            or host == "tiktok.com"
            or host.endswith(".tiktok.com")
        )

        if not allowed:
            await message.answer(
                "Faqat YouTube, Instagram yoki TikTok havolasi."
            )
            return

        folder = tempfile.mkdtemp(prefix="asaved_link_")
        status = await message.answer("⏳ Video yuklanmoqda...")

        try:
            path = await download_link(text, folder)

            if os.path.getsize(path) > MAX_OUTPUT_MB * 1024 * 1024:
                raise RuntimeError("Video 49 MB dan katta.")

            await message.answer_video(FSInputFile(path))
            await status.delete()

        except Exception as e:
            logger.exception("Link yuklash xatosi")
            await status.edit_text(
                f"❌ Yuklab bo‘lmadi: {str(e)[:400]}"
            )

        finally:
            shutil.rmtree(folder, ignore_errors=True)


# ==================================================
# TEXT TO SPEECH
# ==================================================

@dp.callback_query(F.data.startswith("tts:"))
async def tts_callback(call: CallbackQuery):
    uid = call.from_user.id
    text = tts_text.get(uid)

    if not text:
        await call.answer(
            "Avval matn yuboring.",
            show_alert=True
        )
        return

    if not ELEVENLABS_API_KEY:
        await call.answer(
            "ElevenLabs API kaliti sozlanmagan.",
            show_alert=True
        )
        return

    if call.data == "tts:male":
        voice_id = MALE_VOICE_ID
    elif call.data == "tts:female":
        voice_id = FEMALE_VOICE_ID
    else:
        await call.answer("Ovoz turi topilmadi.")
        return

    await call.answer("Ovoz tayyorlanmoqda...")

    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        + voice_id
    )

    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL_ID,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75
        }
    }

    path = None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                json=payload,
                timeout=120
            ) as response:

                if response.status != 200:
                    error = await response.text()
                    raise RuntimeError(error[:400])

                audio = await response.read()

        with tempfile.NamedTemporaryFile(
            suffix=".mp3",
            delete=False
        ) as file:
            file.write(audio)
            path = file.name

        await call.message.answer_audio(
            FSInputFile(path)
        )

        tts_text.pop(uid, None)

    except Exception as e:
        logger.exception("TTS xatosi")
        await call.message.answer(
            f"❌ Ovoz yaratilmadi: {str(e)[:400]}"
        )

    finally:
        if path and os.path.exists(path):
            os.unlink(path)


# ==================================================
# ADMIN XABAR YUBORISH
# ==================================================

@dp.message(Command("broadcast"))
async def broadcast(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    text = (message.text or "").partition(" ")[2].strip()

    if not text:
        await message.answer(
            "Foydalanish: /broadcast xabar matni"
        )
        return

    con = connect_db()
    users = con.execute(
        "SELECT user_id FROM bot_users"
    ).fetchall()
    con.close()

    sent = 0

    for row in users:
        try:
            await bot.send_message(row["user_id"], text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await message.answer(
        f"✅ Xabar {sent} ta foydalanuvchiga yuborildi."
    )


# ==================================================
# MINI APP AUTH
# ==================================================

def validate_init_data(init_data):
    if not init_data:
        return None

    try:
        values = dict(
            parse_qsl(init_data, keep_blank_values=True)
        )

        received_hash = values.pop("hash", None)

        if not received_hash:
            return None

        auth_date = int(values.get("auth_date", "0"))

        if abs(int(time.time()) - auth_date) > 86400:
            return None

        check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(values.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()

        calculated = hmac.new(
            secret_key,
            check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            calculated, received_hash
        ):
            return None

        user = json.loads(values.get("user", "{}"))
        return int(user["id"])

    except Exception:
        return None


async def api_user(request):
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="JSON kerak")

    uid = validate_init_data(
        body.get("initData", "")
    )

    if not uid:
        raise web.HTTPUnauthorized(
            text="Telegram tasdiqlanmadi"
        )

    if not await is_subscribed(uid):
        raise web.HTTPForbidden(
            text="@ReklamaBozoriUzz kanaliga obuna bo‘ling"
        )

    return uid


# ==================================================
# ASAVED CHAT — FAQAT TAKLIF ORQALI 1 GA 1
# ==================================================

def get_session(uid):
    con = connect_db()

    row = con.execute(
        "SELECT * FROM chat_sessions WHERE user_id=?",
        (uid,)
    ).fetchone()

    con.close()

    return dict(row) if row else None


async def create_invite(uid):
    con = connect_db()
    now = int(time.time())

    # Eski takliflarini bekor qilish
    old = con.execute(
        "SELECT room_name FROM chat_sessions WHERE user_id=?",
        (uid,)
    ).fetchone()

    if old:
        con.execute(
            "DELETE FROM chat_invites WHERE owner_id=?",
            (uid,)
        )

    code = secrets.token_urlsafe(12)
    room = "asaved_private_" + secrets.token_urlsafe(12)

    con.execute("""
        INSERT INTO chat_invites
        (code, room_name, owner_id, created_at)
        VALUES (?, ?, ?, ?)
    """, (code, room, uid, now))

    con.execute("""
        INSERT OR REPLACE INTO chat_sessions
        (user_id, status, room_name, updated_at)
        VALUES (?, 'ready', ?, ?)
    """, (uid, room, now))

    con.commit()
    con.close()

    return code


async def join_invite(uid, code):
    con = connect_db()

    invite = con.execute(
        "SELECT * FROM chat_invites WHERE code=?",
        (code,)
    ).fetchone()

    if not invite:
        con.close()
        return False, "Taklif havolasi topilmadi."

    if invite["owner_id"] == uid:
        con.close()
        return False, "O‘z taklifingizga qo‘shila olmaysiz."

    if invite["joined_by"] is not None:
        con.close()
        return False, "Bu taklifdan foydalanilgan."

    now = int(time.time())
    room = invite["room_name"]

    con.execute("""
        INSERT OR REPLACE INTO chat_sessions
        (user_id, status, room_name, updated_at)
        VALUES (?, 'ready', ?, ?)
    """, (uid, room, now))

    con.execute(
        "UPDATE chat_invites SET joined_by=? WHERE code=?",
        (uid, code)
    )

    con.commit()
    con.close()

    return True, "Taklif qabul qilindi."


async def api_invite(request):
    uid = await api_user(request)

    if not LIVEKIT_URL or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        return web.json_response(
            {"error": "LiveKit sozlanmagan"},
            status=503
        )

    code = await create_invite(uid)

    return web.json_response({
        "code": code,
        "link": (
            f"https://t.me/{bot_username}"
            f"?start=invite_{code}"
        )
    })


async def api_join_invite(request):
    uid = await api_user(request)

    body = await request.json()
    code = str(body.get("code", ""))[:100]

    ok, message = await join_invite(uid, code)

    if not ok:
        return web.json_response(
            {"error": message},
            status=400
        )

    return web.json_response({
        "status": "ready",
        "message": message
    })


async def api_token(request):
    uid = await api_user(request)
    session = get_session(uid)

    if not session or session["status"] != "ready":
        return web.json_response(
            {"error": "Avval taklif yarating yoki taklifni qabul qiling."},
            status=409
        )

    room = session["room_name"]

    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True
    )

    token = (
        api.AccessToken(
            LIVEKIT_API_KEY,
            LIVEKIT_API_SECRET
        )
        .with_identity(str(uid))
        .with_name(f"ASAVED-{uid}")
        .with_ttl(timedelta(minutes=30))
        .with_grants(grants)
        .to_jwt()
    )

    return web.json_response({
        "url": LIVEKIT_URL,
        "token": token,
        "room": room
    })


async def api_leave(request):
    uid = await api_user(request)

    con = connect_db()

    session = con.execute(
        "SELECT * FROM chat_sessions WHERE user_id=?",
        (uid,)
    ).fetchone()

    if session:
        room = session["room_name"]

        con.execute(
            "DELETE FROM chat_sessions WHERE user_id=?",
            (uid,)
        )

        if room:
            con.execute(
                "DELETE FROM chat_sessions WHERE room_name=?",
                (room,)
            )
            con.execute(
                "DELETE FROM chat_invites WHERE room_name=?",
                (room,)
            )

    con.commit()
    con.close()

    return web.json_response({"status": "stopped"})


async def api_report(request):
    uid = await api_user(request)
    session = get_session(uid)

    if not session or not session["room_name"]:
        return web.json_response(
            {"error": "Faol suhbat yo‘q"},
            status=400
        )

    room = session["room_name"]

    con = connect_db()

    others = con.execute("""
        SELECT user_id FROM chat_sessions
        WHERE room_name=? AND user_id<>?
    """, (room, uid)).fetchall()

    for other in others:
        con.execute("""
            INSERT INTO chat_reports
            (reporter_id, reported_id, room_name, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            uid,
            other["user_id"],
            room,
            "Foydalanuvchi shikoyati",
            int(time.time())
        ))

    con.commit()
    con.close()

    for admin in ADMIN_IDS:
        try:
            await bot.send_message(
                admin,
                f"⚠️ ASAVED CHAT shikoyati\n"
                f"Shikoyatchi: {uid}\n"
                f"Suhbat: {room}"
            )
        except Exception:
            pass

    return web.json_response({"status": "reported"})


# ==================================================
# MINI APP HTML — APP.PY ICHIDA
# ==================================================

HTML_PAGE = r"""
<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ASAVED CHAT</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js"></script>
<style>
*{box-sizing:border-box}
body{margin:0;background:#10131a;color:white;font-family:Arial}
header{text-align:center;padding:20px;font-size:24px;font-weight:bold}
#status{text-align:center;padding:15px;color:#c5cad5}
#videos{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:10px}
.tile{background:#252b38;border-radius:14px;min-height:170px;overflow:hidden;position:relative}
.tile video{width:100%;height:170px;object-fit:cover}
.tile audio{display:none}
.tile span{position:absolute;bottom:7px;left:7px;background:#0009;padding:5px;border-radius:6px}
.controls{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:12px}
button{border:0;border-radius:12px;padding:15px 8px;font-weight:bold;background:#303849;color:white}
.primary{background:#16a36b}
.danger{background:#d34252}
#invite{padding:12px;word-break:break-all;text-align:center}
.note{text-align:center;color:#9da5b5;font-size:12px;padding:12px}
</style>
</head>
<body>

<header>🌐 ASAVED CHAT</header>
<div id="status">Taklif yaratish yoki taklif orqali kirish.</div>
<div id="videos"></div>

<div class="controls">
<button class="primary" id="inviteBtn">🔗 Taklif yaratish</button>
<button id="connectBtn">▶ Suhbatga ulanish</button>
<button id="micBtn" disabled>🎙 Mikrofon</button>
<button id="camBtn" disabled>📷 Kamera</button>
<button class="danger" id="stopBtn" disabled>⏹ Suhbatni tugatish</button>
<button id="reportBtn" disabled>⚠ Shikoyat</button>
<button id="copyBtn" disabled>📋 Havolani ulashish</button>
</div>

<div id="invite"></div>

<div class="note">
Video suhbat faqat taklif havolasi orqali ishlaydi.
Kamera va mikrofon ruxsatini berishingiz kerak.
</div>

<script>
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

const initData = tg.initData;
const params = new URLSearchParams(location.search);
const inviteCode = params.get("invite");

const statusEl = document.getElementById("status");
const videos = document.getElementById("videos");
const inviteEl = document.getElementById("invite");

let room = null;
let localTracks = [];
let inviteLink = "";

function setStatus(text){
    statusEl.textContent = text;
}

async function request(path, extra={}){
    const response = await fetch(path,{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({initData,...extra})
    });

    const data = await response.json().catch(()=>({}));

    if(!response.ok){
        throw new Error(data.error || "Server xatosi");
    }

    return data;
}

function setConnected(active){
    document.getElementById("connectBtn").disabled = active;
    document.getElementById("stopBtn").disabled = !active;
    document.getElementById("micBtn").disabled = !active;
    document.getElementById("camBtn").disabled = !active;
    document.getElementById("reportBtn").disabled = !active;
}

function addTrack(track, isLocal=false){
    const tile = document.createElement("div");
    tile.className = "tile";

    const element = track.attach();
    tile.appendChild(element);

    const label = document.createElement("span");
    label.textContent = isLocal ? "Siz" : "Suhbatdosh";
    tile.appendChild(label);

    videos.appendChild(tile);
}

async function disconnect(){
    if(room){
        try{
            await room.disconnect();
        }catch(e){}
        room = null;
    }

    localTracks.forEach(track=>{
        try{track.stop()}catch(e){}
    });

    localTracks = [];
    videos.innerHTML = "";
    setConnected(false);
}

async function connectRoom(){
    try{
        const info = await request("/api/token");

        room = new LivekitClient.Room({
            adaptiveStream:true,
            dynacast:true
        });

        room.on(
            LivekitClient.RoomEvent.TrackSubscribed,
            (track)=>{
                addTrack(track,false);
            }
        );

        room.on(
            LivekitClient.RoomEvent.TrackUnsubscribed,
            (track)=>{
                track.detach().forEach(el=>el.remove());
            }
        );

        await room.connect(info.url,info.token);

        localTracks = await LivekitClient.createLocalTracks({
            audio:true,
            video:true
        });

        for(const track of localTracks){
            await room.localParticipant.publishTrack(track);

            if(track.kind==="video"){
                addTrack(track,true);
            }
        }

        room.remoteParticipants.forEach(participant=>{
            participant.trackPublications.forEach(publication=>{
                if(publication.track){
                    addTrack(publication.track,false);
                }
            });
        });

        setConnected(true);
        setStatus("✅ Suhbatga ulandingiz!");

    }catch(e){
        setStatus("❌ Ulanish xatosi: "+e.message);
        await disconnect();
    }
}

document.getElementById("inviteBtn").onclick = async()=>{
    try{
        setStatus("Taklif tayyorlanmoqda...");

        const data = await request("/api/invite");

        inviteLink = data.link;
        inviteEl.textContent = inviteLink;

        document.getElementById("copyBtn").disabled = false;

        setStatus("Taklif tayyor. Suhbatga ulanish mumkin.");

    }catch(e){
        setStatus("❌ "+e.message);
    }
};

document.getElementById("connectBtn").onclick = async()=>{
    try{
        setStatus("Kamera va mikrofon tekshirilmoqda...");

        const test = await navigator.mediaDevices.getUserMedia({
            audio:true,
            video:true
        });

        test.getTracks().forEach(track=>track.stop());

        if(inviteCode){
            setStatus("Taklif tekshirilmoqda...");
            await request("/api/join-invite",{code:inviteCode});
        }

        await connectRoom();

    }catch(e){
        setStatus("❌ "+e.message);
    }
};

document.getElementById("stopBtn").onclick = async()=>{
    await disconnect();

    try{
        await request("/api/leave");
    }catch(e){}

    setStatus("Suhbat tugatildi.");
};

document.getElementById("micBtn").onclick = async()=>{
    if(!room)return;

    const enabled = room.localParticipant.isMicrophoneEnabled;

    await room.localParticipant.setMicrophoneEnabled(!enabled);

    document.getElementById("micBtn").textContent =
        enabled ? "🔇 Mikrofon o‘chiq" : "🎙 Mikrofon yoqilgan";
};

document.getElementById("camBtn").onclick = async()=>{
    if(!room)return;

    const enabled = room.localParticipant.isCameraEnabled;

    await room.localParticipant.setCameraEnabled(!enabled);

    document.getElementById("camBtn").textContent =
        enabled ? "📷 Kamera o‘chiq" : "📷 Kamera yoqilgan";
};

document.getElementById("reportBtn").onclick = async()=>{
    try{
        await request("/api/report");
        setStatus("Shikoyat yuborildi.");
    }catch(e){
        setStatus("❌ "+e.message);
    }
};

document.getElementById("copyBtn").onclick = async()=>{
    if(!inviteLink)return;

    try{
        if(navigator.share){
            await navigator.share({
                title:"ASAVED CHAT",
                url:inviteLink
            });
        }else{
            await navigator.clipboard.writeText(inviteLink);
            setStatus("Havola nusxalandi.");
        }
    }catch(e){
        setStatus(inviteLink);
    }
};

if(inviteCode){
    setStatus("Sizga taklif yuborilgan. Suhbatga ulanish tugmasini bosing.");
}

window.addEventListener("beforeunload",()=>{
    if(room)room.disconnect();
});
</script>
</body>
</html>
"""


# ==================================================
# WEB SERVER
# ==================================================

async def chat_page(request):
    return web.Response(
        text=HTML_PAGE,
        content_type="text/html"
    )


async def health(request):
    return web.json_response({
        "status": "ASAVED is running",
        "channel": CHANNEL_USERNAME
    })


async def on_startup(app):
    global bot_username

    init_db()

    me = await bot.get_me()
    bot_username = me.username or ""

    webhook_url = f"{BASE_URL}/{WEBHOOK_PATH}"

    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET or None,
        drop_pending_updates=False
    )

    logger.info(
        "ASAVED ishga tushdi. Kanal: %s",
        CHANNEL_USERNAME
    )
    logger.info("Webhook: %s", webhook_url)


async def on_cleanup(app):
    try:
        await bot.delete_webhook()
    except Exception:
        pass

    await bot.session.close()


def create_app():
    app = web.Application(
        client_max_size=30 * 1024 * 1024
    )

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/chat", chat_page)

    app.router.add_post("/api/invite", api_invite)
    app.router.add_post("/api/join-invite", api_join_invite)
    app.router.add_post("/api/token", api_token)
    app.router.add_post("/api/leave", api_leave)
    app.router.add_post("/api/report", api_report)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET or None
    ).register(
        app,
        path=f"/{WEBHOOK_PATH}"
    )

    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


if __name__ == "__main__":
    application = create_app()

    web.run_app(
        application,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
    )
