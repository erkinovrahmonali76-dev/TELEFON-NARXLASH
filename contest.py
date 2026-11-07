import hashlib
import hmac
import html
import json
import logging
import os
import random
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot
from aiogram.enums import ChatMemberStatus

log = logging.getLogger("asaved.contest")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "asaved_users.db")
CHANNEL_USERNAME = "@ReklamaBozoriUzz"
INSTAGRAM_URL = "https://www.instagram.com/asavedbot/"
TELEGRAM_URL = "https://t.me/ReklamaBozoriUzz"
CONTEST_TITLE = "ASAVED KONKURS"
PRIZE_TITLE = "Studio Microphone"
WEBAPP_DIR = Path(__file__).parent / "webapp"


def init_contest_db():
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contest_entries (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                instagram_claim INTEGER NOT NULL DEFAULT 0,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def validate_webapp_init_data(init_data: str):
    """Telegram Mini App initData signature validation; rejects forged user IDs."""
    if not BOT_TOKEN or not init_data:
        return None
    try:
        values = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = values.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(values.items())
        )
        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        calculated = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        auth_date = int(values.get("auth_date", "0"))
        if not auth_date or abs(time.time() - auth_date) > 86400:
            return None
        user_data = json.loads(values.get("user", "{}"))
        user_id = int(user_data["id"])
        return {
            "id": user_id,
            "username": user_data.get("username", ""),
            "first_name": user_data.get("first_name", ""),
        }
    except Exception:
        return None


async def get_verified_user(request):
    data = validate_webapp_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if not data:
        raise web.HTTPUnauthorized(text="Telegram orqali oching va qayta urinib ko‘ring.")
    return data


async def contest_page(request):
    return web.FileResponse(WEBAPP_DIR / "index.html")


async def contest_config(request):
    return web.json_response({
        "title": CONTEST_TITLE,
        "prize": PRIZE_TITLE,
        "instagram_url": INSTAGRAM_URL,
        "telegram_url": TELEGRAM_URL,
    })


async def contest_join(request):
    user = await get_verified_user(request)
    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Noto‘g‘ri so‘rov.")
    if payload.get("instagram_followed") is not True:
        raise web.HTTPBadRequest(text="Instagram shartini tasdiqlang.")

    # Telegram obunasi server tomonda qayta tekshiriladi.
    bot: Bot = request.app["contest_bot"]
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user["id"])
        if member.status not in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }:
            return web.json_response(
                {"ok": False, "message": "Telegram kanaliga avval obuna bo‘ling."},
                status=403,
            )
    except Exception:
        log.exception("Contest channel membership check failed")
        return web.json_response(
            {"ok": False, "message": "Obunani tekshirib bo‘lmadi. Kanalda bot admin ekanini tekshiring."},
            status=503,
        )

    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO contest_entries
                (user_id, username, first_name, instagram_claim)
            VALUES (?, ?, ?, 1)
        """, (user["id"], user["username"], user["first_name"]))
        conn.commit()
        row = conn.execute(
            "SELECT joined_at FROM contest_entries WHERE user_id=?",
            (user["id"],)
        ).fetchone()

    return web.json_response({
        "ok": True,
        "message": "Siz konkurs ishtirokchilari ro‘yxatidasiz!",
        "joined_at": row[0] if row else None,
    })


async def contest_status(request):
    user = await get_verified_user(request)
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        row = conn.execute(
            "SELECT joined_at FROM contest_entries WHERE user_id=?",
            (user["id"],)
        ).fetchone()
    return web.json_response({"joined": bool(row), "joined_at": row[0] if row else None})


def setup_contest(app: web.Application, bot: Bot, admin_ids: set[int]):
    init_contest_db()
    app["contest_bot"] = bot
    app["contest_admin_ids"] = admin_ids
    app.router.add_get("/contest", contest_page)
    app.router.add_get("/contest/config", contest_config)
    app.router.add_get("/contest/status", contest_status)
    app.router.add_post("/contest/join", contest_join)
