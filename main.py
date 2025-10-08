import os, re, json, io, tempfile, asyncio, httpx
from dotenv import load_dotenv
load_dotenv()

import asyncpg, pandas as pd
from bs4 import BeautifulSoup
from readability import Document
from duckduckgo_search import DDGS
from openai import OpenAI

from pdfminer.high_level import extract_text as pdf_text
from docx import Document as Docx

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputFile, BotCommand
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler, ContextTypes, filters
)
from aiohttp import web

# ===================== CONFIG =====================
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
DB_URL = os.getenv("DB_URL", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BASE_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
PORT = int(os.getenv("PORT", "10000"))
MEM_LIMIT = int(os.getenv("MEMORY_LIMIT", "1500"))
LANG = os.getenv("LANGUAGE", "ru")
UA = "Mozilla/5.0"

oc = OpenAI(api_key=OPENAI_KEY)
application: Application | None = None

SYS = f"Ты — Jarvis v2.2 Ultimate на {LANG}. Отвечай лаконично, умно и с теплом."


# ===================== DATABASE =====================
async def db_conn():
    return await asyncpg.connect(DB_URL)

async def init_db():
    c = await db_conn()
    await c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            memory JSONB DEFAULT '[]'::jsonb
        )
    """)
    await c.close()


def _user_defaults(uid: int):
    return {
        "user_id": uid,
        "memory": [],
        "mode": "assistant",
        "voice": "alloy",
        "lang": LANG,
        "translate_to": LANG
    }

async def get_user(uid: int):
    try:
        c = await asyncpg.connect(DB_URL)
        row = await c.fetchrow("SELECT memory FROM users WHERE user_id=$1", uid)
        await c.close()
    except:
        row = None
    base = _user_defaults(uid)
    if not row:
        return base
    v = row["memory"]
    if isinstance(v, str):
        try:
            mem = json.loads(v) if v else []
        except:
            mem = []
    else:
        mem = v or []
    base["memory"] = mem
    return base

async def save_memory(uid: int, mem):
    c = await db_conn()
    await c.execute(
        """INSERT INTO users(user_id, memory)
           VALUES ($1, $2)
           ON CONFLICT (user_id)
           DO UPDATE SET memory = excluded.memory""",
        uid, mem
    )
    await c.close()


# ===================== UTILITIES =====================
def ask_openai(messages, temperature=0.3, max_tokens=800):
    r = oc.chat.completions.create(model=MODEL, messages=messages,
                                   temperature=temperature, max_tokens=max_tokens)
    return r.choices[0].message.content.strip()

def read_txt(p): return open(p, "r", encoding="utf-8", errors="ignore").read()
def read_pdf(p): return pdf_text(p) or ""
def read_docx(p): d = Docx(p); return "\n".join([x.text for x in d.paragraphs])
def read_table(p):
    if p.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(p)
    else:
        df = pd.read_csv(p)
    buf = io.StringIO(); df.head(80).to_string(buf)
    return buf.getvalue()

def read_any(p):
    pl = p.lower()
    if pl.endswith((".txt", ".md", ".log")): return read_txt(p)
    if pl.endswith(".pdf"): return read_pdf(p)
    if pl.endswith(".docx"): return read_docx(p)
    if pl.endswith((".csv", ".xlsx", ".xls")): return read_table(p)
    return read_txt(p)

def transcribe(path: str):
    with open(path, "rb") as f:
        r = oc.audio.transcriptions.create(model="whisper-1", file=f)
    return (r.text or "").strip()

def tts_to_mp3(text: str):
    fn = tempfile.mktemp(suffix=".mp3")
    with oc.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts",
        voice="alloy",
        input=text
    ) as resp:
        resp.stream_to_file(fn)
    return fn

def need_web(q: str):
    t = q.lower()
    keys = ["новост", "курс", "цена", "сейчас", "погода", "вышел", "итог"]
    return any(k in t for k in keys)

async def fetch_url(url: str, limit=10000):
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": UA}, timeout=25) as cl:
        r = await cl.get(url)
    html = Document(r.text).summary()
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)[:limit]

async def search_and_fetch(query: str, hits: int = 2):
    links = []
    with DDGS() as ddg:
        for r in ddg.text(query, max_results=hits):
            if r.get("href"): links.append(r["href"])
    out = []
    for u in links[:3]:
        try:
            t = await fetch_url(u)
            if t: out.append(t)
        except:
            pass
    return "\n\n".join(out)[:12000]


# ===================== TELEGRAM UI =====================
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("☀️ Погода", callback_data="weather"),
         InlineKeyboardButton("💸 Курс валют", callback_data="currency")],
        [InlineKeyboardButton("🌍 Новости", callback_data="news"),
         InlineKeyboardButton("🧠 Факт", callback_data="fact")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")]
    ])

async def set_menu(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "запуск"),
        BotCommand("reset", "сбросить память"),
        BotCommand("say", "озвучить текст"),
    ])


# ===================== COMMANDS =====================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await set_menu(ctx.application)
    await update.message.reply_text("Привет, я Jarvis v2.2 Ultimate 🤖", reply_markup=main_menu())

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = await get_user(update.effective_user.id)
    await save_memory(u["user_id"], [])
    await update.message.reply_text("Память очищена 🧹")

async def cmd_say(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2: return await update.message.reply_text("Формат: /say текст")
    text = parts[1].strip()
    mp3 = tts_to_mp3(text)
    try:
        with open(mp3, "rb") as f:
            await update.message.reply_audio(InputFile(f, filename="jarvis.mp3"))
    finally:
        try: os.remove(mp3)
        except: pass


# ===================== HANDLERS =====================
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    text = (update.message.text or "").strip()
    if not text: return
    hist = u["memory"]
    web_snip = ""
    if need_web(text):
        try: web_snip = await search_and_fetch(text)
        except: pass
    msgs = [{"role": "system", "content": SYS}]
    if web_snip:
        msgs.append({"role": "system", "content": f"Актуальная сводка:\n{web_snip}"})
    msgs += hist + [{"role": "user", "content": text}]
    try:
        reply = await asyncio.to_thread(ask_openai, msgs)
    except Exception as e:
        reply = f"⚠️ Ошибка: {e}"
    hist.append({"role": "user", "content": text})
    hist.append({"role": "assistant", "content": reply})
    await save_memory(uid, hist[-MEM_LIMIT:])
    await update.message.reply_text(reply)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    v = update.message.voice or update.message.audio
    if not v: return
    f = await ctx.bot.get_file(v.file_id)
    path = await f.download_to_drive()
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, transcribe, path)
    if not text:
        return await update.message.reply_text("Не удалось распознать голос.")
    uid = update.effective_user.id
    u = await get_user(uid)
    hist = u["memory"]
    msgs = [{"role": "system", "content": SYS}, *hist, {"role": "user", "content": text}]
    try:
        reply = await asyncio.to_thread(ask_openai, msgs)
    except Exception as e:
        reply = f"⚠️ Ошибка: {e}"
    hist.append({"role": "user", "content": text})
    hist.append({"role": "assistant", "content": reply})
    await save_memory(uid, hist[-MEM_LIMIT:])
    mp3 = tts_to_mp3(reply)
    with open(mp3, "rb") as f:
        await update.message.reply_audio(InputFile(f, filename="jarvis.mp3"))
    os.remove(mp3)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "weather":
        await q.edit_message_text("🌤 Введи город: /weather Москва")
    elif data == "currency":
        await q.edit_message_text("💸 Пример: /currency usd")
    elif data == "news":
        await q.edit_message_text("🌍 Пример: /news Россия")
    elif data == "fact":
        await q.edit_message_text("🧠 Пример: /fact")
    elif data == "settings":
        await q.edit_message_text("⚙️ Настройки пока в разработке 🧩")
    else:
        await q.edit_message_text("Неизвестная команда")


# ===================== SERVER =====================
async def health(request):
    return web.Response(text="ok")

async def tg_webhook(request):
    data = await request.json()
    upd = Update.de_json(data, application.bot)
    await application.process_update(upd)
    return web.Response(text="ok")

# ===================== MAIN =====================
def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("say", cmd_say))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

async def main():
    global application
    await init_db()
    application = build_app()
    await application.initialize()
    await application.start()

    aio = web.Application()
    aio.add_routes([web.get("/health", health)])
    aio.add_routes([web.post("/tgwebhook", tg_webhook)])
    runner = web.AppRunner(aio)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)
    await set_menu(application)
    print("READY")
    print("WEBHOOK:", f"{BASE_URL}/tgwebhook", flush=True)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
