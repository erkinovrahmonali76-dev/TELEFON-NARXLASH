ASAVED PRO Telegram Bot

Funksiyalar

• Til tanlash: O‘zbekcha / Русский / English
• @akoshsaved kanaliga obuna tekshiruvi (PRO foydalanuvchilarga tatbiq etilmaydi)
• Dumaloq video
• Video havolasi: YouTube / Instagram / TikTok (ochiq va ruxsat etilgan kontent)
• 9:16 format
• PRO: Instagram video audio trekini MP3 sifatida ajratish
• 7 kun: 30 000 so‘m; 30 kun: 55 000 so‘m
• Chekni admin ko‘rib chiqadi; tasdiqlangach PRO avtomatik faollashadi
• Admin Telegram ID kodi ichida sozlangan

GitHub fayllari

app.py, requirements.txt, Dockerfile, README.md

Render sozlamalari

Docker Web Service ishlating. Environment Variables:

• BOT_TOKEN — BotFather token
• WEBHOOK_SECRET — kamida 16 belgi, A-Z/a-z/0-9/_/-
• WEBHOOK_PATH — asaved-webhook
• PAYMENT_CARD — to‘lov kartasi raqami
• PAYMENT_HOLDER — karta egasining ko‘rsatiladigan ismi

Render avtomatik PORT va RENDER_EXTERNAL_URL beradi.

MUHIM: ma’lumotlar bazasi

Render servisida Persistent Disk yarating:

• Mount Path: /data
• Size: loyihangizga mos (masalan 1 GB)

Bot obunalar va to‘lovlarni /data/asaved.sqlite3 da saqlaydi. Persistent Disk bo‘lmasa deploy/restart paytida ma’lumotlar yo‘qolishi mumkin.

Telegram kanal

@akoshsaved kanalida botni administrator qiling. Aks holda obunani tekshirish ishlamasligi mumkin.

To‘lov xavfsizligi

Chek rasmi to‘lovning o‘zi emas. Admin bank ilovasidan pul tushganini tekshirib, shundan keyingina tasdiqlasin. Karta ma’lumotlarini faqat Render Environment Variables ichida kiriting, GitHub kodiga yozmang.

Kontent

Havoladan faqat ochiq va yuklab olishga, audiosidan foydalanishga ruxsat berilgan kontentni oling. Login/cookie, yopiq profil yoki himoya cheklovlarini chetlab o‘tish qo‘llanmaydi.
