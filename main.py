import os, re, json, io, tempfile, asyncio
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

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputFile, BotCommand
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# === CONFIG ===
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
DB_URL = os.getenv("DB_URL", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
MEM_LIMIT = int(os.getenv("MEMORY_LIMIT", "1500"))
LANG = os.getenv("LANGUAGE", "ru")
BASE_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
VOICE_MODE = os.getenv("VOICE_MODE", "true").lower() == "true"
MIGRATION_KEY = os.getenv("MIGRATION_KEY", "jarvis-fix-123")

UA = "Mozilla/5.0"
SYS = f"Ты Jarvis — ассистент на {LANG}. Отвечай чётко и по делу. Если нужна свежая информация, используй system-сводку."

oc = OpenAI(api_key=OPENAI_KEY)
application: Application | None = None

# === DATABASE ===
async def db_conn(): 
    return await asyncpg.connect(DB_URL)

async def init_db():
    async with await db_conn() as c:
        await c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                memory JSONB DEFAULT '[]'::jsonb
            )
        """)

async def get_memory(uid: int):
    async with await db_conn() as c:
        row = await c.fetchrow("SELECT memory FROM users WHERE user_id=$1", uid)
    if not row:
        return []
    val = row["memory"]
    if isinstance(val, str):
        try: return json.loads(val)
        except: return []
    return val or []

async def save_memory(uid: int, mem):
    async with await db_conn() as c:
        await c.execute("""
            INSERT INTO users (user_id, memory)
            VALUES ($1, $2::jsonb)
            ON CONFLICT (user_id) DO UPDATE SET memory = EXCLUDED.memory
        """, uid, json.dumps(mem))

# === OPENAI ===
def ask_openai(messages, temperature=0.3, max_tokens=800):
    r = oc.chat.completions.create(
        model=MODEL, messages=messages,
        temperature=temperature, max_tokens=max_tokens
    )
    return r.choices[0].message.content.strip()

# === UTILITIES ===
async def fetch_url(url: str, limit=20000):
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": UA}, timeout=25) as cl:
        r = await cl.get(url)
    ct = (r.headers.get("content-type") or "").lower()
    if "text/html" in ct or "<html" in r.text[:500].lower():
        html = Document(r.text).summary()
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n", strip=True)
    else:
        text = r.text
    return re.sub(r"\n{3,}", "\n\n", text)[:limit]

def extract_urls(q: str): 
    return re.findall(r"https?://\S+", q)

async def fetch_urls(urls, limit_chars=12000):
    out = []
    for u in urls[:3]:
        try:
            t = await fetch_url(u, limit=4000)
            if t: out.append(t)
        except Exception:
            pass
    return "\n\n".join(out)[:limit_chars]

async def search_and_fetch(query: str, hits: int = 2, limit_chars: int = 12000):
    links = []
    try:
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=hits, safesearch="moderate"):
                if r and r.get("href"):
                    links.append(r["href"])
    except Exception:
        pass
    return await fetch_urls(links, limit_chars) if links else ""

def read_file(p):
    ext = p.lower()
    if ext.endswith((".txt", ".md", ".log")):
        return open(p, "r", encoding="utf-8", errors="ignore").read()
    if ext.endswith(".pdf"):
        return pdf_text(p)
    if ext.endswith(".docx"):
        d = Docx(p)
        return "\n".join([x.text for x in d.paragraphs])
    if ext.endswith((".csv", ".xlsx", ".xls")):
        df = pd.read_excel(p) if ext.endswith((".xlsx", ".xls")) else pd.read_csv(p)
        b = io.StringIO(); df.head(80).to_string(b)
        return b.getvalue()
    return open(p, "r", encoding="utf-8", errors="ignore").read()

def transcribe(path: str):
    with open(path, "rb") as f:
        r = oc.audio.transcriptions.create(model="whisper-1", file=f)
    return (r.text or "").strip()

def tts_to_mp3(text: str):
    fn = tempfile.mktemp(suffix=".mp3")
    with oc.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts", voice="alloy", input=text, format="mp3"
    ) as resp:
        resp.stream_to_file(fn)
    return fn

# === TELEGRAM HANDLERS ===
async def set_menu(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "запуск"),
        BotCommand("ping", "проверка"),
        BotCommand("read", "прочитать сайт"),
        BotCommand("say", "озвучить текст"),
        BotCommand("reset", "сбросить память"),
    ])

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await set_menu(ctx.application)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Начать", callback_data="start")]])
    await update.message.reply_text("Готов. Пиши вопрос или нажми кнопку.", reply_markup=kb)

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "start":
        await q.edit_message_text("Готов. Пиши вопрос.")

async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await save_memory(update.effective_user.id, [])
    await update.message.reply_text("Память очищена.")

async def cmd_read(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await update.message.reply_text("Формат: /read URL")
    try:
        raw = await fetch_url(parts[1])
    except Exception as e:
        return await update.message.reply_text(f"Ошибка: {e}")
    sys = [{"role": "system", "content": "Суммаризируй текст кратко и структурировано."}]
    out = ask_openai(sys + [{"role": "user", "content": raw[:16000]}]) if len(raw) > 1800 else raw
    await update.message.reply_text(out[:4000])

async def cmd_say(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not VOICE_MODE:
        return await update.message.reply_text("Голос отключен.")
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await update.message.reply_text("Формат: /say текст")
    mp3 = tts_to_mp3(parts[1].strip())
    try:
        with open(mp3, "rb") as f:
            await update.message.reply_audio(InputFile(f, filename="jarvis.mp3"))
    finally:
        os.remove(mp3)

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    if not text:
        return
    hist = await get_memory(uid)
    urls = extract_urls(text)
    web_snip = ""
    if urls:
        web_snip = await fetch_urls(urls)
    elif any(k in text.lower() for k in ["новост", "цена", "сейчас", "погода", "курс"]):
        web_snip = await search_and_fetch(text)
    msgs = [{"role": "system", "content": SYS}]
    if web_snip:
        msgs.append({"role": "system", "content": "Актуальная сводка:\n" + web_snip})
    msgs += hist + [{"role": "user", "content": text}]
    try:
        reply = await asyncio.to_thread(ask_openai, msgs)
    except Exception as e:
        reply = f"⚠️ Ошибка модели: {e}"
    hist += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
    await save_memory(uid, hist[-MEM_LIMIT:])
    await update.message.reply_text(reply)

# === SYSTEM ROUTES ===
async def health(request): 
    return web.Response(text="ok")

async def migrate(request):
    if request.rel_url.query.get("key") != MIGRATION_KEY:
        return web.Response(status=403, text="forbidden")
    c = await asyncpg.connect(DB_URL)
    try:
        await c.execute("BEGIN")
        await c.execute("""
            UPDATE users 
            SET memory='[]' 
            WHERE memory IS NULL 
               OR memory::text='' 
               OR NOT (jsonb_typeof(memory::jsonb) IS NOT NULL)
        """)
        await c.execute("""
            ALTER TABLE users 
            ALTER COLUMN memory TYPE jsonb 
            USING COALESCE(NULLIF(memory::text,''),'[]')::jsonb, 
            ALTER COLUMN memory SET DEFAULT '[]'::jsonb
        """)
        await c.execute("COMMIT")
    except Exception as e:
        await c.execute("ROLLBACK")
        await c.close()
        return web.Response(text=str(e))
    await c.close()
    return web.Response(text="ok")

async def tg_webhook(request):
    try:
        data = await request.json()
        upd = Update.de_json(data, application.bot)
        await application.process_update(upd)
        return web.Response(text="ok")
    except Exception as e:
        return web.Response(status=200, text=str(e))

# === APP SETUP ===
def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("read", cmd_read))
    app.add_handler(CommandHandler("say", cmd_say))
    app.add_handler(CallbackQueryHandler(on_button, pattern="^start$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

async def main():
    global application
    await init_db()
    application = build_app()
    await application.initialize()
    await application.start()
    aio = web.Application()
    aio.add_routes([
        web.get("/health", health),
        web.post("/tgwebhook", tg_webhook),
        web.get("/migrate", migrate),
    ])
    runner = web.AppRunner(aio)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)
    await set_menu(application)
    print("READY"); print("WEBHOOK:", f"{BASE_URL}/tgwebhook", flush=True)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
