import asyncio, html, logging, os, re, sqlite3, tempfile
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Awaitable, Callable
from aiohttp import web, ClientSession, ClientTimeout
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ===================== CONFIG =====================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('kino_olami')
BOT_TOKEN = os.getenv('BOT_TOKEN','').strip()
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET','').strip()
WEBHOOK_PATH = os.getenv('WEBHOOK_PATH','kino-olami-webhook').strip('/')
PORT = int(os.getenv('PORT','10000'))
BASE_URL = os.getenv('RENDER_EXTERNAL_URL','').rstrip('/')
DB_PATH = os.getenv('DB_PATH','kino_olami.db')
CHANNEL_USERNAME = '@kinoolamiuzzzz'
CHANNEL_URL = 'https://t.me/kinoolamiuzzzz'
ADMIN_RAW = os.getenv('ADMIN_IDS','') or os.getenv('ADMIN_ID','')
ADMIN_IDS = {int(x.strip()) for x in ADMIN_RAW.replace(';',',').split(',') if x.strip().isdigit()}
if not BOT_TOKEN: raise RuntimeError('BOT_TOKEN Environment Variable kiritilmagan')
if not WEBHOOK_SECRET or not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', WEBHOOK_SECRET): raise RuntimeError('WEBHOOK_SECRET noto‘g‘ri')
if not BASE_URL: raise RuntimeError('RENDER_EXTERNAL_URL topilmadi')

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(); router = Router(); dp.include_router(router)

# ===================== BUTTONS / STATE =====================
BTN_SEARCH='🎬 Kino qidirish'; BTN_ADD='➕ Kino qo‘shish'; BTN_MY='📁 Mening videolarim'
BTN_ROUND='🎥 Dumaloq video'; BTN_916='📱 9:16 format'; BTN_LINK='🔗 Video havolasi'
BTN_TTS='🎙️ Matnni ovozga aylantirish'; BTN_BROADCAST='📢 Reklamani hammaga yuborish'
BTN_MALE='👨 Erkak ovozi'; BTN_FEMALE='👩 Ayol ovozi'; BTN_MENU='⬅️ Asosiy menyu'
keyboard=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_SEARCH)],[KeyboardButton(text=BTN_ROUND),KeyboardButton(text=BTN_916)],[KeyboardButton(text=BTN_LINK)],[KeyboardButton(text=BTN_TTS)],[KeyboardButton(text=BTN_MY)]],resize_keyboard=True)
voice_keyboard=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_MALE),KeyboardButton(text=BTN_FEMALE)],[KeyboardButton(text=BTN_MENU)]],resize_keyboard=True)
def is_admin(uid): return uid in ADMIN_IDS
def menu_for(uid):
    if not is_admin(uid): return keyboard
    rows=[list(r) for r in keyboard.keyboard]+[[KeyboardButton(text=BTN_ADD)],[KeyboardButton(text=BTN_BROADCAST)]]
    return ReplyKeyboardMarkup(keyboard=rows,resize_keyboard=True)
sub_kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🎬 Kino Olami kanaliga obuna bo‘lish',url=CHANNEL_URL)],[InlineKeyboardButton(text='✅ Obunani tekshirish',callback_data='check_sub')]])
def main_text(): return ('Assalomu alaykum! <b>Kino Olami</b> botiga xush kelibsiz.\n\n🎬 Kino qidirish — kino kodi yoki nomini yuboring.\n📁 Mening videolarim — saqlangan fayllar.\n🎥 Video formatlash va boshqa funksiyalar ham mavjud.')
user_modes:dict[int,str]={}; uploads:dict[int,dict]={}; sub_cache={}; SUB_TTL=30
video_sem=asyncio.Semaphore(2); tts_sem=asyncio.Semaphore(2)
MAX_UPLOAD=20*1024*1024; MAX_OUTPUT=49*1024*1024; MAX_SECONDS=600; MAX_TTS=3000
ALLOWED_HOSTS={'youtube.com','www.youtube.com','m.youtube.com','youtu.be','instagram.com','www.instagram.com','tiktok.com','www.tiktok.com','vm.tiktok.com','vt.tiktok.com'}
ELEVEN_KEY=os.getenv('ELEVENLABS_API_KEY','').strip(); TTS_MODEL=os.getenv('ELEVENLABS_MODEL_ID','eleven_multilingual_v2').strip()
TTS_VOICES={'male':os.getenv('ELEVENLABS_MALE_VOICE_ID','pNInz6obpgDQGcFmaJgB').strip(),'female':os.getenv('ELEVENLABS_FEMALE_VOICE_ID','21m00Tcm4TlvDq8ikWAM').strip()}
AD_TEXT=('🚀 <b>KANALINGIZNI KO‘PROQ INSONLARGA TANITING!</b>\n\nTelegram kanalingiz rivojlanishini xohlaysizmi?\n📣 Reklama uchun admin: @akramovx7')

# ===================== DATABASE =====================
def db(): return sqlite3.connect(DB_PATH,timeout=30)
def init_db():
    Path(DB_PATH).parent.mkdir(parents=True,exist_ok=True)
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS bot_users(user_id INTEGER PRIMARY KEY, first_seen TEXT DEFAULT CURRENT_TIMESTAMP,last_seen TEXT DEFAULT CURRENT_TIMESTAMP)')
        c.execute('CREATE TABLE IF NOT EXISTS ad_sent_users(user_id INTEGER PRIMARY KEY,sent_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        # Barcha katalog elementlari bitta jadvalda: kind=movie yoki series
        c.execute('''CREATE TABLE IF NOT EXISTS catalog(id INTEGER PRIMARY KEY AUTOINCREMENT, code INTEGER UNIQUE NOT NULL, kind TEXT NOT NULL,title TEXT NOT NULL,description TEXT DEFAULT '',poster_file_id TEXT,year INTEGER DEFAULT 0,language TEXT DEFAULT 'O‘zbek',downloads INTEGER DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS episodes(id INTEGER PRIMARY KEY AUTOINCREMENT,catalog_id INTEGER NOT NULL,episode_number INTEGER NOT NULL,file_id TEXT NOT NULL,media_type TEXT DEFAULT 'video',downloads INTEGER DEFAULT 0,UNIQUE(catalog_id,episode_number))''')
        c.execute('''CREATE TABLE IF NOT EXISTS saved_videos(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,file_id TEXT NOT NULL,media_type TEXT NOT NULL,label TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_saved_user ON saved_videos(user_id,id DESC)')
        # Eski app.py dagi movies jadvalidagi bittalik kinolarni yangi katalogga ko‘chirish.
        old_table=c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='movies'").fetchone()
        if old_table:
            cols={r[1] for r in c.execute('PRAGMA table_info(movies)').fetchall()}
            needed={'movie_number','title','file_id'}
            if needed.issubset(cols):
                select_cols=['movie_number','title','description' if 'description' in cols else "''",'poster_file_id' if 'poster_file_id' in cols else 'NULL','year' if 'year' in cols else '0','language' if 'language' in cols else "'O‘zbek'",'downloads' if 'downloads' in cols else '0','file_id','media_type' if 'media_type' in cols else "'video'"]
                for old in c.execute('SELECT '+','.join(select_cols)+' FROM movies').fetchall():
                    code,title,desc,poster,year,language,downloads,fid,media_type=old
                    c.execute('INSERT OR IGNORE INTO catalog(code,kind,title,description,poster_file_id,year,language,downloads) VALUES(?,?,?,?,?,?,?,?)',(code,'movie',title,desc,poster,year,language,downloads))
                    cat=c.execute('SELECT id FROM catalog WHERE code=?',(code,)).fetchone()
                    if cat:
                        c.execute('INSERT OR IGNORE INTO episodes(catalog_id,episode_number,file_id,media_type) VALUES(?,?,?,?)',(cat[0],1,fid,media_type or 'video'))
        c.commit()
def register(uid):
    with db() as c: c.execute('INSERT INTO bot_users(user_id) VALUES(?) ON CONFLICT(user_id) DO UPDATE SET last_seen=CURRENT_TIMESTAMP',(uid,))
def all_users():
    with db() as c: return [r[0] for r in c.execute('SELECT user_id FROM bot_users')]
def save_saved(uid,fid,typ,label):
    with db() as c: c.execute('INSERT INTO saved_videos(user_id,file_id,media_type,label) VALUES(?,?,?,?)',(uid,fid,typ,label[:100]))
def saved_list(uid):
    with db() as c: return c.execute('SELECT id,label FROM saved_videos WHERE user_id=? ORDER BY id DESC LIMIT 15',(uid,)).fetchall()
def saved_get(vid,uid):
    with db() as c: return c.execute('SELECT file_id,media_type,label FROM saved_videos WHERE id=? AND user_id=?',(vid,uid)).fetchone()
def catalog_search(q):
    with db() as c:
        if q.isdigit(): rows=c.execute('SELECT id,code,kind,title FROM catalog WHERE code=?',(int(q),)).fetchall()
        else: rows=c.execute('SELECT id,code,kind,title FROM catalog WHERE title LIKE ? ORDER BY code LIMIT 15',(f'%{q}%',)).fetchall()
        return rows
def catalog_get(cid):
    with db() as c:
        c.row_factory=sqlite3.Row; r=c.execute('SELECT * FROM catalog WHERE id=?',(cid,)).fetchone(); return dict(r) if r else None
def episode_list(cid):
    with db() as c: return c.execute('SELECT id,episode_number FROM episodes WHERE catalog_id=? ORDER BY episode_number',(cid,)).fetchall()
def episode_get(eid):
    with db() as c:
        c.row_factory=sqlite3.Row; r=c.execute('SELECT e.*,c.title,c.code FROM episodes e JOIN catalog c ON c.id=e.catalog_id WHERE e.id=?',(eid,)).fetchone(); return dict(r) if r else None

# ===================== SUBSCRIPTION =====================
async def is_subscribed(uid,cache=True):
    now=asyncio.get_running_loop().time()
    if cache and sub_cache.get(uid,0)>now: return True
    try:
        m=await bot.get_chat_member(CHANNEL_USERNAME,uid)
        ok=m.status in {ChatMemberStatus.MEMBER,ChatMemberStatus.ADMINISTRATOR,ChatMemberStatus.CREATOR}
        if ok: sub_cache[uid]=now+SUB_TTL
        else: sub_cache.pop(uid,None)
        return ok
    except Exception: log.exception('Subscription check failed'); return False
class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self,handler:Callable[[Message,dict[str,Any]],Awaitable[Any]],event:Message,data:dict[str,Any]):
        if not event.from_user: return await handler(event,data)
        register(event.from_user.id)
        if event.text and event.text.split()[0].split('@')[0]=='/myid': return await handler(event,data)
        if await is_subscribed(event.from_user.id): return await handler(event,data)
        await event.answer('⛔ Botdan foydalanish uchun avval Kino Olami kanaliga obuna bo‘ling.',reply_markup=sub_kb); return None
router.message.middleware(SubscriptionMiddleware())
@router.callback_query(F.data=='check_sub')
async def check_sub(c:CallbackQuery):
    if await is_subscribed(c.from_user.id,False):
        await c.answer('✅ Obuna tasdiqlandi!'); await c.message.answer(main_text(),reply_markup=menu_for(c.from_user.id))
    else: await c.answer('❌ Avval kanalga obuna bo‘ling!',show_alert=True)

# ===================== CATALOG OUTPUT =====================
async def send_catalog(chat_id, item):
    eps=episode_list(item['id']) if item['kind']=='series' else []
    cap=(f"🎬 <b>{html.escape(item['title'])}</b>\n━━━━━━━━━━━━━━\n📅 Yili: <b>{item['year'] or 'Ko‘rsatilmagan'}</b>\n🌐 Tili: <b>{html.escape(item['language'] or 'O‘zbek')}</b>\n🎞 Kino kodi: <b>«{item['code']}»</b>\n")
    if item['kind']=='series': cap+=f'📺 Qism: <b>{len(eps)}/{len(eps)}</b>\n'
    cap+=f"📥 Yuklangan: <b>{item['downloads']} marta</b>"
    if item['description']: cap+='\n\n📝 '+html.escape(item['description'])
    rows=[]
    if item['kind']=='series':
        row=[]
        for eid,num in eps:
            row.append(InlineKeyboardButton(text=f'{num}-qism',callback_data=f'ep:{eid}'))
            if len(row)==3: rows.append(row); row=[]
        if row: rows.append(row)
    rows.append([InlineKeyboardButton(text='🔎 Kino kodini qidirish',url=CHANNEL_URL)])
    markup=InlineKeyboardMarkup(inline_keyboard=rows)
    if item['poster_file_id']: await bot.send_photo(chat_id,item['poster_file_id'],caption=cap[:1024],reply_markup=markup)
    else: await bot.send_message(chat_id,cap,reply_markup=markup)
    if item['kind']=='movie':
        with db() as c: row=c.execute('SELECT file_id,media_type FROM episodes WHERE catalog_id=? AND episode_number=1',(item['id'],)).fetchone()
        if row:
            if row[1]=='document': await bot.send_document(chat_id,row[0],caption=f"🎬 {html.escape(item['title'])}")
            else: await bot.send_video(chat_id,row[0],caption=f"🎬 {html.escape(item['title'])}",supports_streaming=True)
@router.callback_query(F.data.startswith('catalog:'))
async def catalog_callback(c:CallbackQuery):
    if not await is_subscribed(c.from_user.id,False): await c.answer('Avval kanalga obuna bo‘ling.',show_alert=True); return
    try: item=catalog_get(int(c.data.split(':')[1]))
    except Exception: item=None
    if not item: await c.answer('Kino topilmadi.',show_alert=True); return
    await c.answer('🎬 Ochilmoqda...')
    with db() as conn: conn.execute('UPDATE catalog SET downloads=downloads+1 WHERE id=?',(item['id'],))
    item['downloads']+=1
    await send_catalog(c.from_user.id,item)
@router.callback_query(F.data.startswith('ep:'))
async def episode_callback(c:CallbackQuery):
    if not await is_subscribed(c.from_user.id,False): await c.answer('Avval kanalga obuna bo‘ling.',show_alert=True); return
    try: ep=episode_get(int(c.data.split(':')[1]))
    except Exception: ep=None
    if not ep: await c.answer('Qism topilmadi.',show_alert=True); return
    await c.answer('📥 Video yuborilmoqda...')
    cap=f"🎬 <b>{html.escape(ep['title'])}</b>\n📺 Qism: <b>{ep['episode_number']}-qism</b>"
    if ep['media_type']=='document': await bot.send_document(c.from_user.id,ep['file_id'],caption=cap)
    else: await bot.send_video(c.from_user.id,ep['file_id'],caption=cap,supports_streaming=True)
    with db() as conn: conn.execute('UPDATE episodes SET downloads=downloads+1 WHERE id=?',(ep['id'],)); conn.execute('UPDATE catalog SET downloads=downloads+1 WHERE id=?',(ep['catalog_id'],))

# ===================== ADMIN ADD WIZARD =====================
@router.message(Command('addmovie'))
@router.message(F.text==BTN_ADD)
async def add_start(m:Message):
    if not is_admin(m.from_user.id): await m.answer('⛔ Faqat admin uchun.'); return
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🎬 Bittalik kino',callback_data='kind:movie')],[InlineKeyboardButton(text='📺 Serial',callback_data='kind:series')]])
    uploads[m.from_user.id]={'step':'kind'}; await m.answer('Qanday kontent qo‘shmoqchisiz?',reply_markup=kb)
@router.callback_query(F.data.startswith('kind:'))
async def choose_kind(c:CallbackQuery):
    if not is_admin(c.from_user.id): await c.answer('Ruxsat yo‘q',show_alert=True); return
    u=uploads.get(c.from_user.id)
    if not u: await c.answer('Jarayon topilmadi',show_alert=True); return
    u['kind']=c.data.split(':')[1]; u['step']='code'; await c.answer(); await c.message.answer('🔢 Kino/serial kodini yuboring:')
@router.message(Command('cancel'))
async def cancel(m:Message): uploads.pop(m.from_user.id,None); await m.answer('❌ Bekor qilindi.',reply_markup=menu_for(m.from_user.id))
@router.message(Command('doneepisodes'))
async def done_episodes(m:Message):
    u=uploads.get(m.from_user.id)
    if not is_admin(m.from_user.id) or not u or u.get('kind')!='series' or u.get('step')!='media' or u.get('count',0)<1: await m.answer('❌ Yakunlash uchun faol serial va kamida bitta qism kerak.'); return
    await m.answer(f"✅ Serial saqlandi!\n🎬 {html.escape(u['title'])}\n🔢 Kod: {u['code']}\n📺 Qismlar: {u['count']} ta",reply_markup=menu_for(m.from_user.id)); uploads.pop(m.from_user.id,None)
@router.message(F.text==BTN_SEARCH)
async def search_start(m:Message): user_modes[m.from_user.id]='search'; await m.answer('🎬 Kino yoki serial nomini/kodini yuboring:')
async def show_results(m,q):
    rows=catalog_search(q); buttons=[]
    for cid,code,kind,title in rows:
        label='📺' if kind=='series' else '🎬'; suffix=' (Serial)' if kind=='series' else ''
        buttons.append([InlineKeyboardButton(text=f'{label} {code}. {title[:38]}{suffix}',callback_data=f'catalog:{cid}')])
    if not buttons: await m.answer('😕 Bunday kino yoki serial topilmadi.'); return
    await m.answer('🔎 Topilgan natijalar:',reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# ===================== MEDIA HELPERS / VIDEO TOOLS =====================
def get_video_info(m):
    if m.video: return m.video.file_id,m.video.file_size,m.video.duration,'video'
    if m.document and (m.document.mime_type or '').startswith('video/'): return m.document.file_id,m.document.file_size,None,'document'
    return None
async def download_tg(m,path):
    info=get_video_info(m)
    if not info: raise ValueError('Video topilmadi')
    fid,size,dur,_=info
    if size and size>MAX_UPLOAD: raise ValueError('Video 20 MB dan katta')
    if dur and dur>MAX_SECONDS: raise ValueError('Video 10 daqiqadan uzun')
    f=await bot.get_file(fid); await bot.download_file(f.file_path,destination=path)
async def ffmpeg(args):
    p=await asyncio.create_subprocess_exec('ffmpeg','-y','-hide_banner','-loglevel','error',*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE); _,err=await p.communicate()
    if p.returncode: raise RuntimeError(err.decode('utf-8','ignore')[-500:])
async def process_format(m,mode):
    status=await m.answer('⚡ Video tayyorlanmoqda...'); uid=m.from_user.id
    try:
        async with video_sem:
            with tempfile.TemporaryDirectory() as td:
                src=Path(td)/'in.mp4'; dst=Path(td)/'out.mp4'; await download_tg(m,src)
                if mode=='round':
                    await ffmpeg(['-i',str(src),'-t','60','-vf','scale=640:640:force_original_aspect_ratio=increase,crop=640:640,setsar=1','-c:v','libx264','-preset','ultrafast','-crf','27','-c:a','aac','-b:a','96k',str(dst)])
                    if dst.stat().st_size>MAX_OUTPUT: raise ValueError('Tayyor video 49 MB dan katta')
                    sent=await m.answer_video_note(FSInputFile(dst)); save_saved(uid,sent.video_note.file_id,'video_note','Dumaloq video')
                else:
                    await ffmpeg(['-i',str(src),'-vf','scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1','-c:v','libx264','-preset','ultrafast','-crf','26','-c:a','aac','-b:a','128k',str(dst)])
                    if dst.stat().st_size>MAX_OUTPUT: raise ValueError('Tayyor video 49 MB dan katta')
                    sent=await m.answer_video(FSInputFile(dst),caption='📱 9:16 format tayyor!'); save_saved(uid,sent.video.file_id,'video','9:16 video')
        await status.delete(); await m.answer('💾 Mening videolarim bo‘limiga saqlandi.')
    except Exception as e: log.exception('format'); await status.edit_text('❌ '+html.escape(str(e)[:300]))
    finally: user_modes.pop(uid,None)

# ===================== LINK DOWNLOAD =====================
def valid_url(raw):
    try:
        p=urlparse(raw.strip()); h=(p.hostname or '').lower(); return p.scheme=='https' and any(h==x or h.endswith('.'+x) for x in ALLOWED_HOSTS)
    except Exception: return False
def platform(url):
    h=urlparse(url).hostname or ''; return 'Instagram' if 'instagram' in h else 'TikTok' if 'tiktok' in h else 'YouTube'
async def download_link(url,folder):
    import yt_dlp
    opts={'format':'bestvideo+bestaudio/best','outtmpl':str(folder/'video.%(ext)s'),'noplaylist':True,'max_filesize':49*1024*1024,'quiet':True,'no_warnings':True,'merge_output_format':'mp4','socket_timeout':20}
    def work():
        with yt_dlp.YoutubeDL(opts) as y: y.download([url])
    await asyncio.to_thread(work); fs=list(folder.glob('video.*'))
    if not fs: raise RuntimeError('Video olinmadi')
    p=max(fs,key=lambda x:x.stat().st_size)
    if p.stat().st_size>49*1024*1024: raise RuntimeError('Video 49 MB dan katta')
    return p

# ===================== TTS =====================
async def tts(text,voice,dst):
    if not ELEVEN_KEY: raise RuntimeError('ELEVENLABS_API_KEY kiritilmagan')
    url=f'https://api.elevenlabs.io/v1/text-to-speech/{voice}'; headers={'xi-api-key':ELEVEN_KEY,'Accept':'audio/mpeg','Content-Type':'application/json'}
    async with ClientSession(timeout=ClientTimeout(total=120)) as s:
        async with s.post(url,headers=headers,json={'text':text,'model_id':TTS_MODEL,'voice_settings':{'stability':0.5,'similarity_boost':0.75}}) as r:
            if r.status!=200: raise RuntimeError(f'ElevenLabs API xatosi: {r.status} {(await r.text())[:150]}')
            data=await r.read()
    if not data: raise RuntimeError('Audio bo‘sh qaytdi')
    dst.write_bytes(data)
async def process_tts(m,text,mode):
    if not text.strip() or len(text)>MAX_TTS: await m.answer(f'Matn 1–{MAX_TTS} belgi bo‘lsin.'); return
    key='male' if mode=='tts_male' else 'female'; status=await m.answer('🎙️ Ovoz yaratilmoqda...')
    try:
        async with tts_sem:
            with tempfile.TemporaryDirectory() as td:
                p=Path(td)/'voice.mp3'; await tts(text,TTS_VOICES[key],p); sent=await m.answer_audio(FSInputFile(p),title='Kino Olami AI ovoz'); save_saved(m.from_user.id,sent.audio.file_id,'audio','AI ovoz')
        await status.delete()
    except Exception as e: log.exception('tts'); await status.edit_text('❌ '+html.escape(str(e)[:300]))
    finally: user_modes.pop(m.from_user.id,None)

# ===================== COMMON COMMANDS / MENUS =====================
@router.message(CommandStart())
async def start(m:Message): user_modes.pop(m.from_user.id,None); await m.answer(main_text(),reply_markup=menu_for(m.from_user.id))
@router.message(Command('myid'))
async def myid(m:Message): await m.answer(f'🆔 Telegram ID: <code>{m.from_user.id}</code>\nADMIN_IDS ga kiriting.')
@router.message(Command('admin'))
async def admin(m:Message):
    if not is_admin(m.from_user.id): await m.answer('⛔ Ruxsat yo‘q.'); return
    await m.answer(f'🛠 Admin panel\n👥 Foydalanuvchilar: {len(all_users())}',reply_markup=menu_for(m.from_user.id))
@router.message(F.text==BTN_MY)
async def my_videos(m:Message):
    rows=saved_list(m.from_user.id)
    if not rows: await m.answer('📁 Saqlangan videolar yo‘q.'); return
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='📹 '+label[:40],callback_data=f'saved:{vid}')] for vid,label in rows]); await m.answer('📁 Saqlangan fayllar:',reply_markup=kb)
@router.callback_query(F.data.startswith('saved:'))
async def saved_cb(c:CallbackQuery):
    try: vid=int(c.data.split(':')[1]); row=saved_get(vid,c.from_user.id)
    except Exception: row=None
    if not row: await c.answer('Topilmadi',show_alert=True); return
    fid,typ,label=row; await c.answer('Yuborilmoqda...')
    if typ=='video_note': await bot.send_video_note(c.from_user.id,fid)
    elif typ=='audio': await bot.send_audio(c.from_user.id,fid)
    elif typ=='document': await bot.send_document(c.from_user.id,fid)
    else: await bot.send_video(c.from_user.id,fid,supports_streaming=True)
@router.message(F.text==BTN_ROUND)
async def round_start(m): user_modes[m.from_user.id]='round'; await m.answer('🎥 60 soniyagacha, 20 MB gacha video yuboring.')
@router.message(F.text==BTN_916)
async def vertical_start(m): user_modes[m.from_user.id]='916'; await m.answer('📱 20 MB gacha video yuboring.')
@router.message(F.text==BTN_LINK)
async def link_start(m): user_modes[m.from_user.id]='link'; await m.answer('🔗 YouTube, Instagram yoki TikTok havolasini yuboring.')
@router.message(F.text==BTN_TTS)
async def tts_start(m): user_modes[m.from_user.id]='voice_choose'; await m.answer('Ovoz turini tanlang:',reply_markup=voice_keyboard)
@router.message(F.text==BTN_MALE)
async def male(m): user_modes[m.from_user.id]='tts_male'; await m.answer('👨 Matn yuboring.',reply_markup=menu_for(m.from_user.id))
@router.message(F.text==BTN_FEMALE)
async def female(m): user_modes[m.from_user.id]='tts_female'; await m.answer('👩 Matn yuboring.',reply_markup=menu_for(m.from_user.id))
@router.message(F.text==BTN_MENU)
async def back(m): user_modes.pop(m.from_user.id,None); await m.answer(main_text(),reply_markup=menu_for(m.from_user.id))

# ===================== BROADCAST =====================
@router.message(F.text==BTN_BROADCAST)
async def broadcast_start(m):
    if not is_admin(m.from_user.id): return
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ Ha',callback_data='bc:yes')],[InlineKeyboardButton(text='❌ Bekor',callback_data='bc:no')]])
    await m.answer(f'{len(all_users())} foydalanuvchiga xabar yuborilsinmi?',reply_markup=kb)
@router.callback_query(F.data.startswith('bc:'))
async def broadcast_cb(c):
    if not is_admin(c.from_user.id): await c.answer('Ruxsat yo‘q',show_alert=True); return
    if c.data=='bc:no': await c.answer('Bekor qilindi'); await c.message.edit_text('❌ Bekor qilindi'); return
    await c.answer('Yuborish boshlandi'); await c.message.edit_text('⏳ Yuborilmoqda...'); sent=fail=0
    for uid in all_users():
        try: await bot.send_message(uid,AD_TEXT); sent+=1
        except Exception: fail+=1
        await asyncio.sleep(.05)
    await c.message.answer(f'✅ Tugadi. Yuborildi: {sent}; Xato: {fail}',reply_markup=menu_for(c.from_user.id))

# ===================== TEXT ROUTER =====================
@router.message(F.text)
async def text_handler(m:Message):
    text=(m.text or '').strip(); uid=m.from_user.id; u=uploads.get(uid); mode=user_modes.get(uid)
    if u:
        step=u.get('step')
        if step=='code':
            if not text.isdigit() or int(text)<1: await m.answer('Musbat raqam yuboring.'); return
            if catalog_search(text): await m.answer('❌ Bu kod band. Boshqa kod yuboring.'); return
            u['code']=int(text); u['step']='title'; await m.answer('🎬 Nomi qanday?'); return
        if step=='title':
            if not text or len(text)>150: await m.answer('Nom 1–150 belgi bo‘lsin.'); return
            u['title']=text; u['step']='year'; await m.answer('📅 Yilini yuboring yoki /skip.'); return
        if step=='year':
            if text=='/skip': u['year']=0
            elif text.isdigit() and 1888<=int(text)<=2100: u['year']=int(text)
            else: await m.answer('Yilni to‘g‘ri kiriting yoki /skip.'); return
            u['step']='language'; await m.answer('🌐 Tilini yozing (O‘zbek, Rus, Ingliz...).'); return
        if step=='language': u['language']=text[:40]; u['step']='description'; await m.answer('📝 Tavsif yoki /skip.'); return
        if step=='description': u['description']='' if text=='/skip' else text[:800]; u['step']='poster'; await m.answer('🖼 Poster yuboring yoki /skip.'); return
        if step=='poster':
            if text!='/skip': await m.answer('Poster yuboring yoki /skip.'); return
            u['poster']=None; await create_catalog_record(m,u); return
        if step=='media': await m.answer('🎞 Video yuboring. Serial tugasa /doneepisodes yozing.'); return
    if mode=='search': user_modes.pop(uid,None); await show_results(m,text); return
    if mode in ('tts_male','tts_female'): await process_tts(m,text,mode); return
    if mode=='voice_choose': await m.answer('Erkak yoki ayol ovozini tanlang.',reply_markup=voice_keyboard); return
    if mode=='link':
        if not valid_url(text): await m.answer('❌ HTTPS YouTube, Instagram yoki TikTok havolasini yuboring.'); return
        status=await m.answer('⚡ Video yuklanmoqda...')
        try:
            with tempfile.TemporaryDirectory() as td:
                async with video_sem: p=await download_link(text,Path(td)); sent=await m.answer_video(FSInputFile(p),caption=f'✅ {platform(text)} videosi')
                save_saved(uid,sent.video.file_id,'video','Havoladan video')
            await status.delete()
        except Exception as e: log.exception('link'); await status.edit_text('❌ '+html.escape(str(e)[:300]))
        finally: user_modes.pop(uid,None)
        return
    if mode in ('round','916'): await m.answer('Galereyadan video yuboring.'); return
    if text.isdigit(): await show_results(m,text); return
    await m.answer('Menyudan kerakli funksiyani tanlang.',reply_markup=menu_for(uid))

async def create_catalog_record(m,u):
    try:
        with db() as c:
            cur=c.execute('INSERT INTO catalog(code,kind,title,description,poster_file_id,year,language) VALUES(?,?,?,?,?,?,?)',(u['code'],u['kind'],u['title'],u['description'],u.get('poster'),u['year'],u['language'])); cid=cur.lastrowid
        u['catalog_id']=cid; u['count']=0; u['step']='media'
        if u['kind']=='movie': await m.answer('🎞 Endi bittalik kino videosini yuboring.')
        else: await m.answer('🎞 Serialning 1-qismini yuboring. Keyingi qismlarni ketma-ket yuboring. Yakunlash: /doneepisodes')
    except sqlite3.IntegrityError: uploads.pop(m.from_user.id,None); await m.answer('❌ Bu kod band.')

@router.message(F.photo)
async def poster_handler(m:Message):
    u=uploads.get(m.from_user.id)
    if not u or u.get('step')!='poster': return
    u['poster']=m.photo[-1].file_id; await create_catalog_record(m,u)

@router.message(F.video | F.document)
async def media_handler(m:Message):
    uid=m.from_user.id; u=uploads.get(uid)
    if u and u.get('step')=='media':
        info=get_video_info(m)
        if not info: await m.answer('❌ Video fayl yuboring.'); return
        fid,size,dur,typ=info
        if size and size>MAX_UPLOAD: await m.answer('❌ Video 20 MB dan katta.'); return
        if u['kind']=='movie' and u['count']>=1: await m.answer('Bu bittalik kino. Bitta video yetarli.'); return
        num=u['count']+1
        with db() as c: c.execute('INSERT INTO episodes(catalog_id,episode_number,file_id,media_type) VALUES(?,?,?,?)',(u['catalog_id'],num,fid,typ))
        u['count']=num
        if u['kind']=='movie':
            uploads.pop(uid,None); await m.answer(f"✅ Bittalik kino saqlandi!\n🎬 {html.escape(u['title'])}\n🔢 Kod: {u['code']}",reply_markup=menu_for(uid))
        else: await m.answer(f'✅ {num}-qism saqlandi. Keyingi qismni yuboring yoki /doneepisodes yozing.')
        return
    mode=user_modes.get(uid)
    if mode in ('round','916'):
        if not get_video_info(m): await m.answer('Video yuboring.'); return
        await process_format(m,mode); return
    await m.answer('Avval menyudan kerakli bo‘limni tanlang.')

@router.message()
async def fallback(m:Message): await m.answer('Menyudan kerakli funksiyani tanlang.',reply_markup=menu_for(m.from_user.id))

# ===================== WEBHOOK =====================
async def on_startup(app):
    init_db(); await bot.set_webhook(f'{BASE_URL}/{WEBHOOK_PATH}',secret_token=WEBHOOK_SECRET,drop_pending_updates=True,allowed_updates=dp.resolve_used_update_types()); log.info('Webhook o‘rnatildi')
async def on_shutdown(app): await bot.delete_webhook(drop_pending_updates=False); await bot.session.close()
async def main():
    init_db(); app=web.Application(client_max_size=2*1024*1024); app.router.add_get('/',lambda r:web.Response(text='Kino Olami bot is running')); app.router.add_get('/health',lambda r:web.Response(text='ok'))
    SimpleRequestHandler(dispatcher=dp,bot=bot,secret_token=WEBHOOK_SECRET).register(app,path=f'/{WEBHOOK_PATH}'); setup_application(app,dp,bot=bot); app.on_startup.append(on_startup); app.on_cleanup.append(on_shutdown)
    runner=web.AppRunner(app); await runner.setup(); site=web.TCPSite(runner,'0.0.0.0',PORT); await site.start(); log.info('Server %s portda',PORT); await asyncio.Event().wait()
if __name__=='__main__': asyncio.run(main())
