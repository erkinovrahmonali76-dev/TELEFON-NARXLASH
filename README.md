
# ASAVED Telegram Bot

ASAVED — video bilan ishlash uchun Telegram bot.

## Bot funksiyalari

1. 🎥 Dumaloq video — galereyadan yuborilgan videoni Telegram dumaloq videosiga aylantiradi.
2. 🔗 Video havolasi — YouTube, Instagram va TikTok havolalaridan ochiq videolarni oladi.
3. 📱 9:16 format — videoni vertikal formatga o‘zgartiradi.

## O‘rnatish

GitHub loyihasida quyidagi fayllar bo‘lishi kerak:

- app.py
- requirements.txt
- Dockerfile
- README.md

## Render sozlamalari

Render'da Docker Web Service yarating.

Environment Variables:

- BOT_TOKEN
- WEBHOOK_SECRET
- WEBHOOK_PATH = asaved-webhook

PORT va RENDER_EXTERNAL_URL Render tomonidan avtomatik beriladi.

## Eslatma

- Galereyadan yuboriladigan video 20 MB dan oshmasin.
- Dumaloq video 60 soniyagacha.
- Video havolalari ochiq bo‘lishi va yuklab olishga ruxsat berilgan bo‘lishi kerak.
- Platforma cheklovlari sabab ayrim havolalar ishlamasligi mumkin.
