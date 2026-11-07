import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from pathlib import Path
from aiohttp import web

COOKIE_NAME = "asaved_admin"
SESSION_SECONDS = 8 * 60 * 60

def _db(db_path, sql, params=(), fetch="none"):
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, params)
        conn.commit()
        if fetch == "one":
            row = cur.fetchone()
            return dict(row) if row else None
        if fetch == "all":
            return [dict(x) for x in cur.fetchall()]
        return cur.rowcount

def init_panel_db(db_path):
    _db(db_path, """CREATE TABLE IF NOT EXISTS panel_channels(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        join_url TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    _db(db_path, """CREATE TABLE IF NOT EXISTS panel_blocks(
        user_id INTEGER PRIMARY KEY,
        reason TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    _db(db_path, """CREATE TABLE IF NOT EXISTS panel_settings(
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""")
    _db(db_path, """CREATE TABLE IF NOT EXISTS panel_admins(
        user_id INTEGER PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

def get_channels(db_path):
    return _db(db_path, "SELECT * FROM panel_channels WHERE enabled=1 ORDER BY id", fetch="all")

def is_blocked(db_path, user_id):
    return bool(_db(db_path, "SELECT user_id FROM panel_blocks WHERE user_id=?", (user_id,), "one"))

def _signature(secret, payload):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

def _make_cookie(secret):
    payload = f"{int(time.time()) + SESSION_SECONDS}:{secrets.token_urlsafe(24)}"
    return payload + "." + _signature(secret, payload)

def _valid_cookie(secret, value):
    try:
        payload, sig = value.rsplit(".", 1)
        expiry, _nonce = payload.split(":", 1)
        return int(expiry) > int(time.time()) and hmac.compare_digest(sig, _signature(secret, payload))
    except Exception:
        return False

def setup_admin_panel(app, bot, db_path, webhook_secret, admin_ids):
    init_panel_db(db_path)
    password = __import__("os").getenv("ADMIN_PANEL_PASSWORD", "").strip()
    if len(password) < 12:
        raise RuntimeError("ADMIN_PANEL_PASSWORD Render Environment'da kamida 12 belgidan iborat bo‘lsin")

    def authorized(request):
        return _valid_cookie(webhook_secret, request.cookies.get(COOKIE_NAME, ""))

    @web.middleware
    async def panel_security(request, handler):
        if request.path.startswith("/admin"):
            if request.path in ("/admin/login", "/admin/logout"):
                return await handler(request)
            if not authorized(request):
                if request.path.startswith("/admin/api/"):
                    raise web.HTTPUnauthorized(text="Sessiya tugagan")
                raise web.HTTPFound("/admin/login")
        return await handler(request)

    # Middleware ro‘yxatiga alohida qo‘shish uchun app yaratilishida ishlatiladi.
    app.middlewares.append(panel_security)

    async def login_page(request):
        return web.Response(text=LOGIN_HTML, content_type="text/html")

    async def login_post(request):
        data = await request.post()
        supplied = str(data.get("password", ""))
        if not hmac.compare_digest(supplied, password):
            return web.Response(text=LOGIN_HTML.replace("XATO_XABAR", "Parol noto‘g‘ri"), content_type="text/html", status=401)
        response = web.HTTPFound("/admin")
        response.set_cookie(COOKIE_NAME, _make_cookie(webhook_secret), httponly=True,
                            secure=True, samesite="Strict", max_age=SESSION_SECONDS, path="/admin")
        raise response

    async def logout(request):
        response = web.HTTPFound("/admin/login")
        response.del_cookie(COOKIE_NAME, path="/admin")
        raise response

    async def dashboard(request):
        stats = {
            "total": _db(db_path, "SELECT COUNT(*) n FROM bot_users", fetch="one")["n"],
            "today": _db(db_path, "SELECT COUNT(*) n FROM bot_users WHERE date(first_seen)=date('now')", fetch="one")["n"],
            "active24": _db(db_path, "SELECT COUNT(*) n FROM bot_users WHERE last_seen >= datetime('now','-1 day')", fetch="one")["n"],
            "blocked": _db(db_path, "SELECT COUNT(*) n FROM panel_blocks", fetch="one")["n"],
            "channels": _db(db_path, "SELECT COUNT(*) n FROM panel_channels WHERE enabled=1", fetch="one")["n"],
        }
        html = DASHBOARD_HTML.replace("__STATS__", json.dumps(stats))
        return web.Response(text=html, content_type="text/html")

    async def api_stats(request):
        return web.json_response({
            "total": _db(db_path, "SELECT COUNT(*) n FROM bot_users", fetch="one")["n"],
            "today": _db(db_path, "SELECT COUNT(*) n FROM bot_users WHERE date(first_seen)=date('now')", fetch="one")["n"],
            "active24": _db(db_path, "SELECT COUNT(*) n FROM bot_users WHERE last_seen >= datetime('now','-1 day')", fetch="one")["n"],
            "blocked": _db(db_path, "SELECT COUNT(*) n FROM panel_blocks", fetch="one")["n"],
            "channels": _db(db_path, "SELECT COUNT(*) n FROM panel_channels WHERE enabled=1", fetch="one")["n"],
        })

    async def api_users(request):
        q = request.query.get("q", "").strip()
        try:
            limit = min(max(int(request.query.get("limit", "100")), 1), 300)
        except ValueError:
            limit = 100
        if q.isdigit():
            rows = _db(db_path, """SELECT u.user_id,u.first_seen,u.last_seen,
                CASE WHEN b.user_id IS NULL THEN 0 ELSE 1 END blocked
                FROM bot_users u LEFT JOIN panel_blocks b ON b.user_id=u.user_id
                WHERE CAST(u.user_id AS TEXT) LIKE ? ORDER BY u.last_seen DESC LIMIT ?""",
                (f"%{q}%", limit), "all")
        else:
            rows = _db(db_path, """SELECT u.user_id,u.first_seen,u.last_seen,
                CASE WHEN b.user_id IS NULL THEN 0 ELSE 1 END blocked
                FROM bot_users u LEFT JOIN panel_blocks b ON b.user_id=u.user_id
                ORDER BY u.last_seen DESC LIMIT ?""", (limit,), "all")
        return web.json_response(rows)

    async def api_user_action(request):
        data = await request.json()
        uid = int(data.get("user_id", 0))
        action = data.get("action")
        if uid <= 0 or action not in ("block", "unblock"):
            raise web.HTTPBadRequest(text="Noto‘g‘ri so‘rov")
        if action == "block":
            _db(db_path, "INSERT OR REPLACE INTO panel_blocks(user_id,reason) VALUES(?,?)",
                (uid, str(data.get("reason", "Admin tomonidan"))[:200]))
        else:
            _db(db_path, "DELETE FROM panel_blocks WHERE user_id=?", (uid,))
        return web.json_response({"ok": True})

    async def api_channels(request):
        return web.json_response(_db(db_path, "SELECT * FROM panel_channels ORDER BY id DESC", fetch="all"))

    async def api_add_channel(request):
        data = await request.json()
        chat_id = str(data.get("chat_id", "")).strip()
        join_url = str(data.get("join_url", "")).strip()
        title = str(data.get("title", "")).strip()
        if not chat_id or not join_url.startswith("https://t.me/") or not title:
            raise web.HTTPBadRequest(text="Kanal ID, nomi va https://t.me/ havolasi kerak")
        try:
            chat = await bot.get_chat(chat_id)
            await bot.get_chat_member(chat.id, bot.id)
        except Exception:
            raise web.HTTPBadRequest(text="Bot kanalni topolmadi. Botni kanalga admin qilib qo‘shing va ID ni tekshiring.")
        _db(db_path, "INSERT OR REPLACE INTO panel_channels(chat_id,title,join_url,enabled) VALUES(?,?,?,1)",
            (str(chat.id), title[:100], join_url))
        return web.json_response({"ok": True})

    async def api_delete_channel(request):
        cid = int(request.match_info["channel_id"])
        _db(db_path, "DELETE FROM panel_channels WHERE id=?", (cid,))
        return web.json_response({"ok": True})

    async def api_broadcast(request):
        data = await request.json()
        text = str(data.get("text", "")).strip()
        if not text or len(text) > 4000:
            raise web.HTTPBadRequest(text="Xabar 1–4000 belgi bo‘lishi kerak")
        users = _db(db_path, "SELECT user_id FROM bot_users", fetch="all")
        sent = failed = 0
        for row in users:
            try:
                await bot.send_message(int(row["user_id"]), text, disable_web_page_preview=True)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.04)
        return web.json_response({"total": len(users), "sent": sent, "failed": failed})

    async def api_settings_get(request):
        rows = _db(db_path, "SELECT key,value FROM panel_settings", fetch="all")
        values = {x["key"]: x["value"] for x in rows}
        return web.json_response(values)

    async def api_settings_post(request):
        data = await request.json()
        allowed = {"welcome_text", "ad_text", "bot_description"}
        for key, value in data.items():
            if key in allowed and isinstance(value, str) and len(value) <= 4000:
                _db(db_path, "INSERT OR REPLACE INTO panel_settings(key,value) VALUES(?,?)", (key, value))
        return web.json_response({"ok": True})

    async def api_admins(request):
        rows = _db(db_path, "SELECT user_id,created_at FROM panel_admins ORDER BY created_at", fetch="all")
        return web.json_response([{"user_id": int(x["user_id"]), "created_at": x["created_at"]} for x in rows])

    async def api_add_admin(request):
        data = await request.json()
        uid = int(data.get("user_id", 0))
        if uid <= 0:
            raise web.HTTPBadRequest(text="Telegram ID noto‘g‘ri")
        _db(db_path, "INSERT OR IGNORE INTO panel_admins(user_id) VALUES(?)", (uid,))
        return web.json_response({"ok": True})

    async def api_remove_admin(request):
        uid = int(request.match_info["user_id"])
        if uid in admin_ids:
            raise web.HTTPForbidden(text="Environment orqali berilgan asosiy adminni bu yerdan o‘chirib bo‘lmaydi")
        _db(db_path, "DELETE FROM panel_admins WHERE user_id=?", (uid,))
        return web.json_response({"ok": True})

    routes = [
        web.get("/admin/login", login_page), web.post("/admin/login", login_post),
        web.get("/admin/logout", logout), web.get("/admin", dashboard),
        web.get("/admin/api/stats", api_stats), web.get("/admin/api/users", api_users),
        web.post("/admin/api/user-action", api_user_action),
        web.get("/admin/api/channels", api_channels), web.post("/admin/api/channels", api_add_channel),
        web.delete("/admin/api/channels/{channel_id}", api_delete_channel),
        web.post("/admin/api/broadcast", api_broadcast),
        web.get("/admin/api/settings", api_settings_get), web.post("/admin/api/settings", api_settings_post),
        web.get("/admin/api/admins", api_admins), web.post("/admin/api/admins", api_add_admin),
        web.delete("/admin/api/admins/{user_id}", api_remove_admin),
    ]
    app.add_routes(routes)


LOGIN_HTML = r"""<!doctype html><html lang="uz"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ASAVED Admin</title>
<style>*{box-sizing:border-box}body{margin:0;background:#090e17;color:#edf2fa;font:16px Arial;display:grid;min-height:100vh;place-items:center}.card{width:min(92%,390px);background:#151d29;border:1px solid #293548;border-radius:22px;padding:28px}input,button{width:100%;padding:14px;border-radius:12px;border:1px solid #35445a;background:#0d1520;color:white;margin-top:12px}button{background:#e9a928;color:#111;font-weight:bold;border:0}small{color:#ff8b8b}</style>
<div class="card"><h2>ASAVED · Admin</h2><p>Web Panelga kirish</p><small>XATO_XABAR</small><form method="post" action="/admin/login"><input name="password" type="password" placeholder="Admin paroli" required><button>Kirish</button></form></div></html>"""

DASHBOARD_HTML = r"""<!doctype html><html lang="uz"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0b1018"><title>ASAVED Admin Panel</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0b1018;color:#e8edf5;font:15px Inter,Arial,sans-serif}.top{padding:24px 5%;border-bottom:1px solid #263141;display:flex;justify-content:space-between;align-items:center}.top h1{margin:0;font-size:25px}.wrap{max-width:1150px;margin:auto;padding:25px 4% 100px}.tabs{display:flex;gap:8px;overflow:auto;padding-bottom:18px}.tabs button,.action{background:#192331;color:#dce5f2;border:1px solid #2c3a4d;padding:12px 16px;border-radius:12px;white-space:nowrap;cursor:pointer}.tabs button.active,.primary{background:#e6a72b;color:#151515;border-color:#e6a72b;font-weight:700}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:14px}.card{background:#151d28;border:1px solid #293647;border-radius:18px;padding:20px;margin-bottom:16px}.muted{color:#8795a7}.num{font-size:36px;font-weight:750;margin:10px 0}.section{display:none}.section.active{display:block}input,textarea,select{width:100%;padding:13px;margin:7px 0 12px;border-radius:11px;border:1px solid #334256;background:#0d1520;color:#fff}textarea{min-height:110px;resize:vertical}label{color:#aebaca}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.row>*{flex:1}button{cursor:pointer}.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:500px}td,th{text-align:left;padding:12px;border-bottom:1px solid #2a3544}th{color:#93a2b4}.tag{color:#62d89a}.danger{background:#4b222b!important;color:#ffd9dd!important}.small{font-size:12px}.logout{color:#f1bd55;text-decoration:none}@media(max-width:600px){.top{padding:18px 5%}.wrap{padding:18px 4% 90px}.card{padding:16px}.num{font-size:30px}}
</style></head><body>
<header class="top"><h1>ASAVED <span class="muted">/ Admin</span></h1><a class="logout" href="/admin/logout">Chiqish ↗</a></header>
<main class="wrap">
<nav class="tabs"><button class="active" data-tab="dash">Dashboard</button><button data-tab="users">Ishtirokchilar</button><button data-tab="channels">Kanallar</button><button data-tab="broadcast">Xabar yuborish</button><button data-tab="settings">Dizayn / Matn</button><button data-tab="admins">Adminlar</button></nav>
<section id="dash" class="section active"><h2>Boshqaruv</h2><div class="grid">
<div class="card"><div class="muted">Jami foydalanuvchilar</div><div class="num" id="total">—</div></div>
<div class="card"><div class="muted">Bugun qo‘shilgan</div><div class="num" id="today">—</div></div>
<div class="card"><div class="muted">Oxirgi 24 soat faol</div><div class="num" id="active24">—</div></div>
<div class="card"><div class="muted">Bloklangan</div><div class="num" id="blocked">—</div></div>
<div class="card"><div class="muted">Majburiy kanallar</div><div class="num" id="channelsCount">—</div></div></div>
<div class="card"><b>Tezkor ma’lumot</b><p class="muted">Panel orqali foydalanuvchilar, majburiy obuna kanallari, xabarlar va bot matnlarini boshqaring.</p></div></section>
<section id="users" class="section"><h2>Foydalanuvchilar</h2><div class="card"><div class="row"><input id="userSearch" placeholder="Telegram ID bo‘yicha qidirish"><button class="action" onclick="loadUsers()">Qidirish</button></div><div class="tablewrap"><table><thead><tr><th>ID</th><th>Birinchi kirgan</th><th>Oxirgi faollik</th><th>Holat</th><th>Amal</th></tr></thead><tbody id="usersRows"></tbody></table></div></div></section>
<section id="channels" class="section"><h2>Majburiy obuna kanallari</h2><div class="card"><h3>Kanal qo‘shish</h3><label>Kanal ID yoki @username</label><input id="channelId" placeholder="-1001234567890 yoki @kanal"><label>Kanal nomi</label><input id="channelTitle" placeholder="ASAVED kanal"><label>Obuna havolasi</label><input id="channelUrl" placeholder="https://t.me/kanal"><button class="action primary" onclick="addChannel()">Kanalni qo‘shish</button><p class="muted small">Bot kanalga administrator qilib qo‘shilgan bo‘lishi kerak. Private kanal uchun -100... ID va taklif havolasini kiriting.</p></div><div class="card"><div class="tablewrap"><table><thead><tr><th>Nomi</th><th>ID</th><th>Havola</th><th></th></tr></thead><tbody id="channelsRows"></tbody></table></div></div></section>
<section id="broadcast" class="section"><h2>Ommaviy xabar</h2><div class="card"><label>Yuboriladigan xabar (HTML format qo‘llab-quvvatlanadi)</label><textarea id="broadcastText" placeholder="Assalomu alaykum!"></textarea><button class="action primary" onclick="broadcast()">Hammaga yuborish</button><p id="broadcastResult" class="muted"></p></div></section>
<section id="settings" class="section"><h2>Bot matnlari va dizayni</h2><div class="card"><label>Salomlashuv matni</label><textarea id="welcome_text"></textarea><label>Reklama matni</label><textarea id="ad_text"></textarea><label>Bot tavsifi</label><textarea id="bot_description"></textarea><button class="action primary" onclick="saveSettings()">Saqlash</button><p class="muted small">Matnlar bazaga saqlanadi. Bot kodi ularni o‘qishi uchun app.py dagi main_text va AD_TEXT o‘qish funksiyalarini ulash kerak.</p></div></section>
<section id="admins" class="section"><h2>Panel adminlari</h2><div class="card"><label>Yangi admin Telegram ID</label><input id="newAdminId" inputmode="numeric" placeholder="123456789"><button class="action primary" onclick="addAdmin()">Admin qo‘shish</button></div><div class="card" id="adminsRows"></div><p class="muted small">Bu ro‘yxat Web Panel boshqaruvchilarini qayd etadi. Bot ichidagi admin huquqi uchun ADMIN_IDS Environment qiymatini ham sozlang.</p></section>
</main>
<script>
const $=id=>document.getElementById(id);
async function api(url,opts={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...opts});if(!r.ok){let t=await r.text();throw new Error(t||('HTTP '+r.status))}return r.json()}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.section').forEach(x=>x.classList.remove('active'));b.classList.add('active');$(b.dataset.tab).classList.add('active');if(b.dataset.tab==='users')loadUsers();if(b.dataset.tab==='channels')loadChannels();if(b.dataset.tab==='settings')loadSettings();if(b.dataset.tab==='admins')loadAdmins()});
async function loadStats(){let s=await api('/admin/api/stats');$('total').textContent=s.total;$('today').textContent=s.today;$('active24').textContent=s.active24;$('blocked').textContent=s.blocked;$('channelsCount').textContent=s.channels}
async function loadUsers(){let q=encodeURIComponent($('userSearch').value||'');let a=await api('/admin/api/users?q='+q);$('usersRows').innerHTML=a.map(u=>`<tr><td>${u.user_id}</td><td>${u.first_seen||'—'}</td><td>${u.last_seen||'—'}</td><td>${u.blocked?'Bloklangan':'Faol'}</td><td><button class="action ${u.blocked?'':'danger'}" onclick="userAction(${u.user_id},'${u.blocked?'unblock':'block'}')">${u.blocked?'Ochish':'Bloklash'}</button></td></tr>`).join('')}
async function userAction(id,action){await api('/admin/api/user-action',{method:'POST',body:JSON.stringify({user_id:id,action})});loadUsers();loadStats()}
async function loadChannels(){let a=await api('/admin/api/channels');$('channelsRows').innerHTML=a.map(c=>`<tr><td>${esc(c.title)}</td><td>${esc(c.chat_id)}</td><td><a href="${esc(c.join_url)}" target="_blank">Ochish</a></td><td><button class="action danger" onclick="deleteChannel(${c.id})">O‘chirish</button></td></tr>`).join('')}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function addChannel(){try{await api('/admin/api/channels',{method:'POST',body:JSON.stringify({chat_id:$('channelId').value,title:$('channelTitle').value,join_url:$('channelUrl').value})});$('channelId').value=$('channelTitle').value=$('channelUrl').value='';await loadChannels();await loadStats();alert('Kanal qo‘shildi')}catch(e){alert(e.message)}}
async function deleteChannel(id){if(!confirm('Kanalni o‘chirasizmi?'))return;await api('/admin/api/channels/'+id,{method:'DELETE'});loadChannels();loadStats()}
async function broadcast(){if(!confirm('Xabar barcha foydalanuvchilarga yuborilsinmi?'))return;$('broadcastResult').textContent='Yuborilmoqda...';try{let x=await api('/admin/api/broadcast',{method:'POST',body:JSON.stringify({text:$('broadcastText').value})});$('broadcastResult').textContent=`Tugadi. Jami ${x.total}, yuborildi ${x.sent}, xato ${x.failed}`}catch(e){$('broadcastResult').textContent=e.message}}
async function loadSettings(){let x=await api('/admin/api/settings');['welcome_text','ad_text','bot_description'].forEach(k=>$(k).value=x[k]||'')}
async function saveSettings(){await api('/admin/api/settings',{method:'POST',body:JSON.stringify(Object.fromEntries(['welcome_text','ad_text','bot_description'].map(k=>[k,$(k).value])))});alert('Saqlandi')}
async function loadAdmins(){let a=await api('/admin/api/admins');$('adminsRows').innerHTML=a.map(x=>`<p>${x.user_id} <button class="action danger" onclick="removeAdmin(${x.user_id})">O‘chirish</button></p>`).join('')||'Hali qo‘shimcha admin yo‘q'}
async function addAdmin(){let id=$('newAdminId').value;await api('/admin/api/admins',{method:'POST',body:JSON.stringify({user_id:id})});$('newAdminId').value='';loadAdmins()}
async function removeAdmin(id){await api('/admin/api/admins/'+id,{method:'DELETE'});loadAdmins()}
loadStats().catch(e=>alert(e.message));
</script></body></html>"""
