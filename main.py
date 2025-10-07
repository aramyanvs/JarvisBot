import os, re, json, io, tempfile, asyncio, math
from dotenv import load_dotenv
load_dotenv()

import asyncpg, httpx, pandas as pd
from bs4 import BeautifulSoup
from readability import Document
from duckduckgo_search import DDGS
from aiohttp import web
from openai import OpenAI
from pdfminer.high_level import extract_text as pdf_text
from docx import Document as Docx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, BotCommand
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
DB_URL = os.getenv("DB_URL", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
MEM_LIMIT = int(os.getenv("MEMORY_LIMIT", "1500"))
LANG = os.getenv("LANGUAGE", "ru")
BASE_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
VOICE_MODE = os.getenv("VOICE_MODE", "true").lower() == "true"
MIGRATION_KEY = os.getenv("MIGRATION_KEY", "jarvis-fix-123")

UA = "Mozilla/5.0"
SYSTEM_PREFIX = f"Ты Jarvis — ассистент на {LANG}. Отвечай кратко, чётко и по делу."

oc = OpenAI(api_key=OPENAI_KEY)
application = None

async def db_conn():
    return await asyncpg.connect(DB_URL)

async def init_db():
    c = await db_conn()
    try:
        await c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                memory JSONB DEFAULT '[]'::jsonb
            );
        """)
        await c.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY,
                mode TEXT DEFAULT 'friendly',
                tts BOOLEAN DEFAULT true,
                language TEXT DEFAULT 'ru',
                last_seen TIMESTAMP DEFAULT NOW()
            );
        """)
        await c.execute("""
            CREATE TABLE IF NOT EXISTS vectors (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                content TEXT,
                embedding JSONB,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
    finally:
        await c.close()

async def migrate_db():
    c = await db_conn()
    try:
        await c.execute("BEGIN")
        await c.execute("UPDATE users SET memory='[]' WHERE memory IS NULL OR memory::text='' OR NOT (jsonb_typeof(COALESCE(memory::jsonb,'[]'::jsonb)) IS NOT NULL)")
        await c.execute("ALTER TABLE users ALTER COLUMN memory TYPE jsonb USING COALESCE(NULLIF(memory::text,''),'[]')::jsonb, ALTER COLUMN memory SET DEFAULT '[]'::jsonb")
        await c.execute("COMMIT")
    except Exception:
        await c.execute("ROLLBACK")
    finally:
        await c.close()

async def get_memory(uid:int):
    c = await db_conn()
    try:
        r = await c.fetchrow("SELECT memory FROM users WHERE user_id=$1", uid)
    finally:
        await c.close()
    if not r: return []
    v = r["memory"]
    if isinstance(v, str):
        try: return json.loads(v)
        except: return []
    return v or []

async def save_memory(uid:int, mem):
    c = await db_conn()
    try:
        await c.execute("INSERT INTO users(user_id,memory) VALUES($1,$2::jsonb) ON CONFLICT(user_id) DO UPDATE SET memory=EXCLUDED.memory", uid, json.dumps(mem, ensure_ascii=False))
    finally:
        await c.close()

async def get_settings(uid:int):
    c = await db_conn()
    try:
        r = await c.fetchrow("SELECT mode, tts, language FROM user_settings WHERE user_id=$1", uid)
        if r:
            return {"mode": r["mode"], "tts": r["tts"], "language": r["language"]}
        await c.execute("INSERT INTO user_settings(user_id,mode,tts,language) VALUES($1,'friendly',true,'ru') ON CONFLICT DO NOTHING", uid)
        return {"mode":"friendly", "tts":True, "language":LANG}
    finally:
        await c.close()

async def set_setting(uid:int, key:str, value):
    c = await db_conn()
    try:
        if key == "mode":
            await c.execute("INSERT INTO user_settings(user_id,mode) VALUES($1,$2) ON CONFLICT (user_id) DO UPDATE SET mode=EXCLUDED.mode", uid, value)
        elif key == "tts":
            await c.execute("INSERT INTO user_settings(user_id,tts) VALUES($1,$2) ON CONFLICT (user_id) DO UPDATE SET tts=EXCLUDED.tts", uid, value)
        elif key == "language":
            await c.execute("INSERT INTO user_settings(user_id,language) VALUES($1,$2) ON CONFLICT (user_id) DO UPDATE SET language=EXCLUDED.language", uid, value)
    finally:
        await c.close()

async def save_vector(uid:int, text:str, emb):
    c = await db_conn()
    try:
        await c.execute("INSERT INTO vectors(user_id,content,embedding) VALUES($1,$2,$3::jsonb)", uid, text, json.dumps(emb))
    finally:
        await c.close()

async def fetch_vectors(uid:int, limit=200):
    c = await db_conn()
    try:
        rows = await c.fetch("SELECT id,content,embedding FROM vectors WHERE user_id=$1 ORDER BY created_at DESC LIMIT $2", uid, limit)
    finally:
        await c.close()
    out=[]
    for r in rows:
        emb = r["embedding"]
        if isinstance(emb, str):
            try: emb = json.loads(emb)
            except: emb = []
        out.append({"id": r["id"], "content": r["content"], "embedding": emb})
    return out

def cosine(a,b):
    if not a or not b: return 0.0
    s=0.0; sa=0.0; sb=0.0
    for x,y in zip(a,b):
        s+=x*y; sa+=x*x; sb+=y*y
    if sa==0 or sb==0: return 0.0
    return s/(math.sqrt(sa)*math.sqrt(sb))

def ask_openai(messages, temperature=0.3, max_tokens=800):
    r = oc.chat.completions.create(model=MODEL, messages=messages, temperature=temperature, max_tokens=max_tokens)
    return r.choices[0].message.content.strip()

async def embed_text(text:str):
    r = oc.embeddings.create(model=EMBED_MODEL, input=text)
    return r.data[0].embedding if hasattr(r,"data") else []

async def fetch_url(url:str, limit=20000):
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent":UA}, timeout=25) as cl:
        r = await cl.get(url)
    ct = (r.headers.get("content-type") or "").lower()
    txt = r.text or ""
    if "text/html" in ct or "<html" in txt[:500].lower():
        html = Document(txt).summary()
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n", strip=True)
    else:
        text = txt
    return re.sub(r"\n{3,}", "\n\n", text)[:limit]

def need_web(q:str):
    t = q.lower()
    keys = ["сейчас","сегодня","новост","курс","цена","погода","обнов","вышел","итог","сколько","когда","расписан","матч","акции"]
    return any(k in t for k in keys) or "http" in t or re.search(r"\b20(2[4-9]|3\d)\b", t) is not None

def extract_urls(q:str): return re.findall(r"https?://\S+", q)

async def fetch_urls(urls, limit_chars=12000):
    out=[]
    for u in urls[:3]:
        try:
            t = await fetch_url(u, limit=4000)
            if t: out.append(t)
        except: pass
    return "\n\n".join(out)[:limit_chars]

async def search_and_fetch(query:str, hits:int=3, limit_chars:int=12000):
    links=[]
    try:
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=hits, safesearch="moderate"):
                if r and r.get("href"): links.append(r["href"])
    except: pass
    return await fetch_urls(links, limit_chars) if links else ""

def transcribe(path:str):
    with open(path,"rb") as f:
        r = oc.audio.transcriptions.create(model="whisper-1", file=f)
    return (r.text or "").strip()

def tts_to_mp3(text:str):
    fn = tempfile.mktemp(suffix=".mp3")
    with oc.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts",
        voice="alloy",
        input=text,
        format="mp3"
    ) as resp:
        resp.stream_to_file(fn)
    return fn

async def set_menu(app):
    await app.bot.set_my_commands([
        BotCommand("start","запуск"),
        BotCommand("ping","проверка"),
        BotCommand("read","прочитать сайт"),
        BotCommand("say","озвучить текст"),
        BotCommand("reset","очистить память"),
        BotCommand("mode","сменить режим"),
        BotCommand("profile","профиль")
    ])

async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await set_menu(ctx.application)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Начать", callback_data="start")]])
    await update.message.reply_text("Я Jarvis. Готов к работе.", reply_markup=kb)

async def on_button(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "start":
        await q.edit_message_text("Пиши сообщение или пришли файл/голосовое.")

async def cmd_ping(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_profile(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = await get_settings(uid)
    await update.message.reply_text(f"Режим: {s['mode']}\nTTS: {s['tts']}\nЯзык: {s['language']}")

async def cmd_mode(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧠 Эксперт", callback_data="mode_expert"), InlineKeyboardButton("😎 Шутник", callback_data="mode_joker")],
            [InlineKeyboardButton("🧘 Философ", callback_data="mode_philos"), InlineKeyboardButton("❤️ Дружелюбный", callback_data="mode_friendly")]
        ])
        return await update.message.reply_text("Выбери режим:", reply_markup=kb)
    mode = parts[1].strip().lower()
    uid = update.effective_user.id
    await set_setting(uid, "mode", mode)
    await update.message.reply_text(f"Режим установлен: {mode}")

async def on_mode_button(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data
    if data.startswith("mode_"):
        m = {"mode_expert":"expert","mode_joker":"joker","mode_philos":"philos","mode_friendly":"friendly"}.get(data, "friendly")
        await set_setting(uid, "mode", m)
        await q.edit_message_text(f"Режим установлен: {m}")

async def cmd_reset(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await save_memory(uid, [])
    await update.message.reply_text("Память очищена.")

async def cmd_read(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts)<2:
        return await update.message.reply_text("Формат: /read URL")
    try:
        raw = await fetch_url(parts[1])
    except Exception as e:
        return await update.message.reply_text(f"Ошибка: {e}")
    sys = [{"role":"system","content":"Суммаризируй текст кратко и структурированно."}]
    out = ask_openai(sys+[{"role":"user","content":raw[:16000]}]) if len(raw)>1800 else raw
    await update.message.reply_text(out[:4000])

async def cmd_say(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts)<2:
        return await update.message.reply_text("Формат: /say текст")
    mp3 = tts_to_mp3(parts[1].strip())
    try:
        with open(mp3,"rb") as f:
            await update.message.reply_audio(InputFile(f, filename="jarvis.mp3"))
    finally:
        try: os.remove(mp3)
        except: pass

async def on_voice(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not VOICE_MODE: return
    v = update.message.voice or update.message.audio
    if not v: return
    f = await ctx.bot.get_file(v.file_id)
    path = await f.download_to_drive()
    text = await asyncio.to_thread(transcribe, path)
    if not text:
        return await update.message.reply_text("Не удалось распознать голос.")
    uid = update.effective_user.id
    hist = await get_memory(uid)
    s = await get_settings(uid)
    msgs = [{"role":"system","content":SYSTEM_PREFIX + f" Режим: {s['mode']}"}] + hist + [{"role":"user","content":text}]
    try:
        reply = await asyncio.to_thread(ask_openai, msgs)
    except Exception as e:
        reply = f"Ошибка модели: {e}"
    hist += [{"role":"user","content":text},{"role":"assistant","content":reply}]
    await save_memory(uid, hist[-MEM_LIMIT:])
    if s["tts"]:
        mp3 = tts_to_mp3(reply)
        try:
            with open(mp3,"rb") as f:
                await update.message.reply_audio(InputFile(f, filename="jarvis.mp3"))
        finally:
            try: os.remove(mp3)
            except: pass
    else:
        await update.message.reply_text(reply)

async def semantic_search(uid:int, query:str, top=3):
    qemb = await embed_text(query)
    rows = await fetch_vectors(uid, limit=500)
    scored=[]
    for r in rows:
        emb = r["embedding"]
        sc = cosine(qemb, emb) if emb else 0.0
        scored.append((sc, r["content"]))
    scored.sort(key=lambda x: x[0], reverse=True)
    return "\n\n".join([c for s,c in scored[:top] if s>0.6])

async def on_text(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or update.message.caption or "").strip()
    if not text: return
    s = await get_settings(uid)
    urls = extract_urls(text)
    web_snip = ""
    if urls:
        try: web_snip = await fetch_urls(urls)
        except: web_snip = ""
    elif need_web(text):
        try: web_snip = await search_and_fetch(text, hits=3)
        except: web_snip = ""
    sem = await semantic_search(uid, text)
    hist = await get_memory(uid)
    msgs = [{"role":"system","content":SYSTEM_PREFIX + f" Режим: {s['mode']}"}]
    if web_snip: msgs.append({"role":"system","content":"Актуальная сводка:\n"+web_snip})
    if sem: msgs.append({"role":"system","content":"Похожие ваши прошлые диалоги:\n"+sem})
    msgs += hist + [{"role":"user","content":text}]
    try:
        reply = await asyncio.to_thread(ask_openai, msgs)
    except Exception as e:
        reply = f"Ошибка модели: {e}"
    hist += [{"role":"user","content":text},{"role":"assistant","content":reply}]
    await save_memory(uid, hist[-MEM_LIMIT:])
    emb = await embed_text(text)
    await save_vector(uid, text, emb)
    if s["tts"]:
        mp3 = tts_to_mp3(reply)
        try:
            with open(mp3,"rb") as f:
                await update.message.reply_audio(InputFile(f, filename="jarvis.mp3"))
        finally:
            try: os.remove(mp3)
            except: pass
    else:
        await update.message.reply_text(reply)

async def health(request): 
    return web.Response(text="ok")

async def migrate(request):
    if request.rel_url.query.get("key") != MIGRATION_KEY:
        return web.Response(status=403, text="forbidden")
    try:
        await migrate_db()
        return web.Response(text="ok")
    except Exception as e:
        return web.Response(text=str(e))

async def tg_webhook(request):
    try:
        data = await request.json()
        upd = Update.de_json(data, application.bot)
        await application.process_update(upd)
        return web.Response(text="ok")
    except Exception as e:
        return web.Response(status=200, text=str(e))

def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("read", cmd_read))
    app.add_handler(CommandHandler("say", cmd_say))
    app.add_handler(CallbackQueryHandler(on_button, pattern="^start$"))
    app.add_handler(CallbackQueryHandler(on_mode_button, pattern="^mode_"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

async def startup_checks():
    missing = []
    if not BOT_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
    if not OPENAI_KEY: missing.append("OPENAI_API_KEY")
    if not DB_URL: missing.append("DB_URL")
    if missing:
        print("MISSING ENV:", missing)
        return False
    return True

async def main():
    global application
    ok = await startup_checks()
    if not ok:
        print("Startup failed due to missing env vars")
        return
    await init_db()
    application = build_app()
    await application.initialize()
    await application.start()
    aio = web.Application()
    aio.add_routes([web.get("/health", health), web.post("/tgwebhook", tg_webhook), web.get("/migrate", migrate)])
    runner = web.AppRunner(aio); await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT); await site.start()
    if BASE_URL:
        try:
            await application.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)
        except Exception as e:
            print("Webhook set error:", e)
    await set_menu(application)
    print("READY"); print("WEBHOOK:", f"{BASE_URL}/tgwebhook", flush=True)
    await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(main())
