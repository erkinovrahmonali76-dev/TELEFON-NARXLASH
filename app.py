import asyncio
import base64
import html
import logging
import os
import re
import secrets
import time
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("asaved")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "asaved-webhook").strip("/")
PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN Environment Variable kiritilmagan")
if not WEBHOOK_SECRET or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET kamida 16 ta belgi bo‘lsin; faqat harf, raqam, _ yoki - ishlating")
if not BASE_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL topilmadi. Render Web Service ishlatayotganingizni tekshiring")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

BTN_ROUND = "🎥 Dumaloq video"
BTN_LINK = "🔗 Video havolasi"
BTN_916 = "📱 9:16 format"
BTN_PRANK = "😂 Do‘stga Prank"

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_ROUND), KeyboardButton(text=BTN_916)],
        [KeyboardButton(text=BTN_LINK)],
        [KeyboardButton(text=BTN_PRANK)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Kerakli funksiyani tanlang"
)

# Foydalanuvchining keyingi amali.
user_modes: dict[int, str] = {}
prank_names: dict[int, str] = {}

# Prank sahifalari RAM'da saqlanadi. Render qayta ishga tushsa, havolalar yangidan yaratiladi.
# Har bir sahifa 24 soatgacha ishlaydi.
PRANK_TTL_SECONDS = 24 * 60 * 60
MAX_PRANK_IMAGE_BYTES = 5 * 1024 * 1024
prank_pages: dict[str, dict] = {}

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
}

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 49 * 1024 * 1024
MAX_LINK_BYTES = 49 * 1024 * 1024
MAX_SECONDS = 600


def main_text() -> str:
    return (
        "Assalomu alaykum! <b>ASAVED</b> botiga xush kelibsiz.\\n\\n"
        "🎥 <b>Dumaloq video</b> — galereyadan 60 soniyagacha video yuboring.\\n"
        "📱 <b>9:16 format</b> — galereyadan video yuboring.\\n"
        "🔗 <b>Video havolasi</b> — YouTube, Instagram yoki TikTok havolasini yuboring.\\n"
        "😂 <b>Do‘stga Prank</b> — do‘stingiz uchun kulgili prank havolasi yarating.\\n\\n"
        "Faqat o‘zingizga tegishli yoki foydalanishga ruxsatingiz bor media fayllardan foydalaning."
    )


def cleanup_prank_pages():
    now = time.time()
    expired = [
        token for token, data in prank_pages.items()
        if now - data["created_at"] > PRANK_TTL_SECONDS
    ]
    for token in expired:
        prank_pages.pop(token, None)


@router.message(CommandStart())
async def start(message: Message):
    user_modes.pop(message.from_user.id, None)
    prank_names.pop(message.from_user.id, None)
    await message.answer(main_text(), reply_markup=keyboard)


@router.message(F.text == BTN_ROUND)
async def choose_round(message: Message):
    user_modes[message.from_user.id] = "round"
    await message.answer("🎥 Endi galereyadan 60 soniyagacha video yuboring.")


@router.message(F.text == BTN_916)
async def choose_916(message: Message):
    user_modes[message.from_user.id] = "916"
    await message.answer("📱 Endi 9:16 formatga o‘tkazish uchun videoni yuboring.")


@router.message(F.text == BTN_LINK)
async def choose_link(message: Message):
    user_modes[message.from_user.id] = "link"
    await message.answer("🔗 YouTube, Instagram yoki TikTok video havolasini yuboring.")


@router.message(F.text == BTN_PRANK)
async def choose_prank(message: Message):
    user_modes[message.from_user.id] = "prank_name"
    prank_names.pop(message.from_user.id, None)
    await message.answer(
        "😂 <b>DO‘STGA PRANK</b>\\n\\n"
        "1️⃣ Do‘stingizning ismini yozing.\\n"
        "Keyin uning rasmini yuborasiz.\\n\\n"
        "Eslatma: faqat ulashishga ruxsatingiz bor rasmni yuboring. "
        "Prank sahifasining oxirida bu hazil ekani ochiq ko‘rsatiladi."
    )


def get_video_info(message: Message):
    if message.video:
        return message.video.file_id, message.video.file_size, message.video.duration, "video.mp4"
    if message.document and (message.document.mime_type or "").startswith("video/"):
        return message.document.file_id, message.document.file_size, None, message.document.file_name or "video.mp4"
    return None


async def download_telegram_video(message: Message, target: Path) -> tuple[Path, int | None]:
    info = get_video_info(message)
    if not info:
        raise ValueError("Video topilmadi. Iltimos, video fayl yuboring.")
    file_id, size, duration, filename = info
    if size and size > MAX_UPLOAD_BYTES:
        raise ValueError("Video 20 MB dan katta. Telegram bot orqali bunday faylni olish imkoni yo‘q; kichikroq video yuboring.")
    if duration and duration > MAX_SECONDS:
        raise ValueError("Video 10 daqiqadan uzun. Qisqaroq video yuboring.")
    tg_file = await bot.get_file(file_id)
    await bot.download_file(tg_file.file_path, destination=target)
    return target, duration


async def run_ffmpeg(args: list[str]):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", "ignore")[-1200:] or "FFmpeg xatoligi")


async def make_round(src: Path, dst: Path):
    await run_ffmpeg([
        "-i", str(src),
        "-t", "60",
        "-vf", "scale=640:640:force_original_aspect_ratio=increase,crop=640:640,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        "-movflags", "+faststart", str(dst)
    ])


async def make_916(src: Path, dst: Path):
    await run_ffmpeg([
        "-i", str(src),
        "-vf", "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(dst)
    ])


async def process_uploaded_video(message: Message, mode: str):
    status = await message.answer("⏳ Video tayyorlanmoqda...")
    try:
        with tempfile.TemporaryDirectory(prefix="asaved_") as tmp:
            folder = Path(tmp)
            src = folder / "input_video"
            dst = folder / "output.mp4"
            await download_telegram_video(message, src)
            if mode == "round":
                await make_round(src, dst)
                if dst.stat().st_size > MAX_OUTPUT_BYTES:
                    raise ValueError("Tayyor video juda katta chiqdi. Qisqaroq video yuboring.")
                await message.answer_video_note(FSInputFile(dst))
            else:
                await make_916(src, dst)
                if dst.stat().st_size > MAX_OUTPUT_BYTES:
                    raise ValueError("Tayyor video 49 MB dan katta. Qisqaroq video yuboring.")
                await message.answer_video(FSInputFile(dst), caption="📱 9:16 format tayyor!")
        await status.delete()
    except Exception as exc:
        log.exception("Video processing failed")
        await status.edit_text(f"❌ Xatolik: {str(exc)[:700]}")
    finally:
        user_modes.pop(message.from_user.id, None)


def valid_video_url(raw: str) -> bool:
    try:
        parsed = urlparse(raw.strip())
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and (
            host in ALLOWED_HOSTS or any(host.endswith("." + h) for h in ALLOWED_HOSTS)
        )
    except Exception:
        return False


async def download_public_link(url: str, folder: Path) -> Path:
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp o‘rnatilmagan. requirements.txt faylini tekshiring.")

    output_template = str(folder / "link_video.%(ext)s")
    options = {
        "format": "best[ext=mp4][height<=1080]/best[height<=1080]/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "max_filesize": MAX_LINK_BYTES,
        "socket_timeout": 25,
        "retries": 2,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }

    def do_download():
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

    await asyncio.to_thread(do_download)
    files = [p for p in folder.iterdir() if p.is_file() and p.name.startswith("link_video.")]
    if not files:
        raise RuntimeError("Video topilmadi yoki platforma yuklashga ruxsat bermadi.")
    result = max(files, key=lambda p: p.stat().st_size)
    if result.stat().st_size > MAX_LINK_BYTES:
        raise RuntimeError("Video 49 MB dan katta. Boshqa, qisqaroq video havolasini yuboring.")
    return result


@router.message(F.text)
async def handle_text(message: Message):
    text = (message.text or "").strip()
    user_id = message.from_user.id
    mode = user_modes.get(user_id)

    if mode == "prank_name":
        if not 2 <= len(text) <= 40:
            await message.answer("Ism 2 tadan 40 tagacha belgidan iborat bo‘lsin. Qayta yozing.")
            return
        prank_names[user_id] = text
        user_modes[user_id] = "prank_photo"
        await message.answer(
            f"✅ Ism qabul qilindi: <b>{html.escape(text)}</b>\\n\\n"
            "Endi do‘stingizning rasmini <b>foto</b> ko‘rinishida yuboring."
        )
        return

    if mode == "prank_photo":
        await message.answer("📸 Iltimos, rasmni Telegram'ga foto qilib yuboring.")
        return

    if mode == "link":
        if not valid_video_url(text):
            await message.answer("❌ Faqat HTTPS YouTube, Instagram yoki TikTok video havolasini yuboring.")
            return
        status = await message.answer("⏳ Havola tekshirilmoqda va video olinmoqda...")
        try:
            with tempfile.TemporaryDirectory(prefix="asaved_link_") as tmp:
                video_path = await download_public_link(text, Path(tmp))
                await message.answer_video(FSInputFile(video_path), caption="✅ Video tayyor!")
            await status.delete()
        except Exception:
            log.exception("Link download failed")
            await status.edit_text(
                "❌ Videoni olib bo‘lmadi. Havola yopiq, platforma cheklovi yoki video hajmi sabab bo‘lishi mumkin. "
                "O‘zingizga tegishli yoki yuklashga ruxsatingiz bor ochiq videoni sinab ko‘ring."
            )
        finally:
            user_modes.pop(user_id, None)
        return

    if mode in ("round", "916"):
        await message.answer("Iltimos, havola emas, galereyadan video fayl yuboring.")
        return

    if valid_video_url(text):
        await message.answer("Avval 🔗 Video havolasi tugmasini bosing, keyin havolani yuboring.")
    else:
        await message.answer("Menyudan kerakli funksiyani tanlang.", reply_markup=keyboard)


@router.message(F.photo)
async def handle_prank_photo(message: Message):
    user_id = message.from_user.id
    if user_modes.get(user_id) != "prank_photo":
        await message.answer("Rasm bilan ishlash uchun avval menyudan kerakli funksiyani tanlang.", reply_markup=keyboard)
        return

    photo = message.photo[-1]
    if photo.file_size and photo.file_size > MAX_PRANK_IMAGE_BYTES:
        await message.answer("❌ Rasm 5 MB dan katta. Iltimos, kichikroq rasm yuboring.")
        return

    status = await message.answer("⏳ Prank sahifasi tayyorlanmoqda...")
    try:
        tg_file = await bot.get_file(photo.file_id)
        import io
        buffer = io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=buffer)
        image_bytes = buffer.getvalue()
        if not image_bytes or len(image_bytes) > MAX_PRANK_IMAGE_BYTES:
            raise ValueError("Rasm hajmi 5 MB dan katta yoki rasmni olib bo‘lmadi.")

        cleanup_prank_pages()
        token = secrets.token_urlsafe(18)
        prank_pages[token] = {
            "name": prank_names.get(user_id, "Do‘st"),
            "image": base64.b64encode(image_bytes).decode("ascii"),
            "created_at": time.time(),
        }
        link = f"{BASE_URL}/prank/{token}"
        user_modes.pop(user_id, None)
        prank_names.pop(user_id, None)

        await status.edit_text(
            "🎉 <b>Prank tayyor!</b>\\n\\n"
            f"Do‘stingizga yuborish uchun havola:\\n{link}\\n\\n"
            "⏳ Havola 24 soatgacha ishlaydi. Sahifadagi oxirgi qadamda hazil ekani ochiq aytiladi."
        )
    except Exception as exc:
        log.exception("Prank creation failed")
        await status.edit_text(f"❌ Prank yaratilmadi: {html.escape(str(exc)[:300])}")
        user_modes.pop(user_id, None)
        prank_names.pop(user_id, None)


@router.message(F.video | F.document)
async def handle_video(message: Message):
    mode = user_modes.get(message.from_user.id)
    if mode not in ("round", "916"):
        await message.answer("Avval 🎥 Dumaloq video yoki 📱 9:16 format tugmasini tanlang.")
        return
    if not get_video_info(message):
        await message.answer("Iltimos, video fayl yuboring.")
        return
    await process_uploaded_video(message, mode)


@router.message()
async def fallback(message: Message):
    await message.answer("Menyudan kerakli funksiyani tanlang.", reply_markup=keyboard)


async def prank_page(request: web.Request):
    token = request.match_info["token"]
    cleanup_prank_pages()
    data = prank_pages.get(token)
    if not data:
        return web.Response(
            text="<h2>Bu prank havolasi muddati tugagan yoki mavjud emas.</h2>",
            content_type="text/html",
            status=404,
        )

    safe_name = html.escape(data["name"])
    image_data = data["image"]
    page = f"""<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ASAVED Prank</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;min-height:100vh;background:#10131c;color:#fff;
font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;padding:20px}}
.card{{width:100%;max-width:440px;background:#1b2030;border:1px solid #343b50;border-radius:22px;
padding:24px;text-align:center;box-shadow:0 18px 55px #0008}}
.tag{{color:#aab5d1;font-size:12px;letter-spacing:2px}} h1{{font-size:26px;margin:14px 0}}
p{{color:#d5d9e5;line-height:1.5}} img{{width:100%;max-height:360px;object-fit:contain;border-radius:14px;
background:#0c0f17;margin:12px 0}} button{{width:100%;border:0;border-radius:13px;padding:15px;
font-size:16px;font-weight:bold;background:#6d7cff;color:white;cursor:pointer;margin-top:12px}}
.small{{font-size:12px;color:#9da6bb;margin-top:16px}}
</style></head>
<body><main class="card">
<div class="tag">ASAVED • DO‘STGA PRANK</div>
<section id="step">
<h1>{safe_name}, sizga noodatiy xabar bor! 😳</h1>
<p>Bu rasm siznikimi?</p>
<img src="data:image/jpeg;base64,{image_data}" alt="Yuborilgan rasm">
<button onclick="nextStep()">Keyingisini ko‘rish</button>
</section>
<p class="small">Bu sahifa hazil uchun tayyorlangan. Hech qanday ma’lumot yig‘ilmaydi.</p>
</main>
<script>
const screens = [
  ["Bir daqiqa... 👀", "Siz maxsus ASAVED prank sinoviga tushdingiz."],
  ["Oxirgi tekshiruv! 😮", "Kulishga tayyormisiz? Keyingi tugmani bosing."],
  ["HAZIL EDI, UKA! 😂", "Qo‘rqib ketdingmi? Do‘sting sizga prank tayyorladi! 🤣"]
];
let index = 0;
function nextStep() {{
  const section = document.getElementById("step");
  if (index < screens.length) {{
    const item = screens[index++];
    section.innerHTML = "<h1>" + item[0] + "</h1><p>" + item[1] + "</p>" +
      (index === screens.length ? "<div style='font-size:54px'>🤣😂🤣</div>" : "") +
      "<button onclick='nextStep()'>" + (index === screens.length ? "Yakunlash" : "Keyingisini ko‘rish") + "</button>";
  }} else {{
    section.innerHTML = "<h1>Prank tugadi! 😂</h1><p>ASAVED orqali tayyorlangan hazil.</p>";
  }}
}}
</script></body></html>"""
    return web.Response(text=page, content_type="text/html")


async def on_startup(app):
    webhook_url = f"{BASE_URL}/{WEBHOOK_PATH}"
    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types()
    )
    log.info("Webhook set: %s", webhook_url)


async def on_shutdown(app):
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.session.close()


async def main():
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_get("/", lambda request: web.Response(text="ASAVED bot is running"))
    app.router.add_get("/health", lambda request: web.Response(text="ok"))
    app.router.add_get("/prank/{token}", prank_page)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET
    ).register(app, path=f"/{WEBHOOK_PATH}")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_shutdown)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("ASAVED listening on port %s", PORT)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
