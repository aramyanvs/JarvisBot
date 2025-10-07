import os, re, json, tempfile, asyncio
from datetime import datetime
import asyncpg
import httpx
from bs4 import BeautifulSoup
from readability import Document
from duckduckgo_search import DDGS
from aiohttp import web
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
DB_URL = os.getenv("DB_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
MEM_LIMIT = int(os.getenv("MEMORY_LIMIT", "1500"))
VOICE_MODE = os.getenv("VOICE_MODE", "true").lower() == "true"
LANG = os.getenv("LANGUAGE", "ru")
BASE_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
UA = "Mozilla/5.0"
SYS = f"Ты Jarvis — ассистент на {LANG}. Отвечай кратко и по делу. Если нужна актуальная информация, используй сводку из system."

application: Application | None = None
oc = OpenAI(api_key=OPENAI_KEY)

async def db_conn():
    return await asyncpg.connect(DB_URL)

async def init_db():
    c = await db_conn()
    await c.execute("create table if not exists users (user_id bigint primary key, memory jsonb default '[]'::jsonb)")
    await c.close()

async def get_memory(uid: int):
    c = await db_conn()
    r = await c.fetchrow("select memory from users where user_id=$1", uid)
    await c.close()
    return r["memory"] if r else []

async def save_memory(uid: int, mem):
    c = await db_conn()
    await c.execute(
        "insert into users(user_id,memory) values($1,$2) on conflict(user_id) do update set memory=excluded.memory",
        uid, mem
    )
    await c.close()

async def reset_memory(uid: int):
    c = await db_conn()
    await c.execute("delete from users where user_id=$1", uid)
    await c.close()

def ask_openai(messages, temperature=0.3, max_tokens=800):
    r = oc.chat.completions.create(model=MODEL, messages=messages, temperature=temperature, max_tokens=max_tokens)
    return r.choices[0].message.content.strip()

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

def need_web(q: str):
    t = q.lower()
    keys = ["сейчас","сегодня","новост","курс","цена","сколько стоит","когда будет","последн","обнов","релиз","погода","расписан","матч","акции","доступно","вышел","итог"]
    if any(k in t for k in keys): return True
    if re.search(r"\b20(2[4-9]|3\d)\b", t): return True
    if "http://" in t or "https://" in t: return True
    return False

def extract_urls(q: str):
    return re.findall(r"https?://\S+", q)

async def fetch_urls(urls, limit_chars=12000):
    out = []
    for u in urls[:3]:
        try:
            t = await fetch_url(u, limit=4000)
            if t: out.append(t)
        except:
            pass
    return "\n\n".join(out)[:limit_chars]

async def search_and_fetch(query: str, hits: int = 2, limit_chars: int = 12000):
    links = []
    try:
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=hits, safesearch="moderate"):
                if r and r.get("href"):
                    links.append(r["href"])
    except:
        pass
    full = await fetch_urls(links, limit_chars) if links else ""
    return full

def read_any(path: str):
    p = path.lower()
    if p.endswith(".pdf"):
        from pdfminer.high_level import extract_text as pdf_text
        return pdf_text(path)
    if p.endswith(".docx"):
        from docx import Document as Docx
        d = Docx(path)
        return "\n".join([p.text for p in d.paragraphs])
    return open(path, "r", encoding="utf-8", errors="ignore").read()

def transcribe(path: str):
    with open(path, "rb") as f:
        r = oc.audio.transcriptions.create(model="whisper-1", file=f)
    return r.text or ""

def tts_to_mp3(text: str):
    fn = tempfile.mktemp(suffix=".mp3")
    r = oc.audio.speech.create(model="tts-1", voice="alloy", input=text, format="mp3")
    with open(fn, "wb") as f:
        f.write(r.read())
    return fn

async def set_menu(app: Application):
    from telegram import BotCommand
    cmds = [
        BotCommand("start", "перезапуск"),
        BotCommand("ping", "проверка"),
        BotCommand("reset", "забыть контекст"),
        BotCommand("read", "прочитать URL"),
        BotCommand("say", "озвучить текст")
    ]
    try:
        await app.bot.set_my_commands(cmds)
    except:
        pass

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("Меню", callback_data="start")]]
    await update.effective_message.reply_text("Я на связи. Пиши вопрос.", reply_markup=InlineKeyboardMarkup(kb))

async def ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("pong")

async def do_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reset_memory(update.effective_user.id)
    await update.effective_message.reply_text("Память очищена.")

async def read_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.effective_message.reply_text("Формат: /read URL")
    try:
        raw = await fetch_url(ctx.args[0])
    except Exception as e:
        return await update.effective_message.reply_text(f"Ошибка: {e}")
    sys = [{"role": "system", "content": "Суммаризируй текст кратко и структурировано."}]
    out = ask_openai(sys + [{"role": "user", "content": raw[:16000]}]) if len(raw) > 1800 else raw
    await update.effective_message.reply_text(out[:4000])

async def say_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not VOICE_MODE:
        return await update.effective_message.reply_text("Голос отключен")
    txt = " ".join(ctx.args) if ctx.args else ""
    if not txt:
        return await update.effective_message.reply_text("Формат: /say текст")
    fn = tts_to_mp3(txt)
    try:
        await update.effective_message.reply_audio(audio=open(fn, "rb"))
    finally:
        try: os.remove(fn)
        except: pass

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("ОК")

async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not VOICE_MODE:
        return await update.effective_message.reply_text("Голос отключен")
    m = update.effective_message
    f = (m.voice or m.audio)
    if not f: return
    file = await ctx.bot.get_file(f.file_id)
    fn = tempfile.mktemp(suffix=".ogg")
    await file.download_to_drive(fn)
    try:
        txt = transcribe(fn)
    finally:
        try: os.remove(fn)
        except: pass
    await update.effective_message.reply_text(txt or "пусто")

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    if not text: return
    web_snip = ""
    urls = extract_urls(text)
    if urls:
        try: web_snip = await fetch_urls(urls)
        except: web_snip = ""
    elif need_web(text):
        try: web_snip = await search_and_fetch(text, hits=2)
        except: web_snip = ""
    hist = await get_memory(uid)
    msgs = [{"role": "system", "content": SYS}]
    if web_snip:
        msgs.append({"role": "system", "content": "Актуальная сводка из интернета:\n" + web_snip})
    msgs += hist + [{"role": "user", "content": text}]
    reply = await asyncio.to_thread(ask_openai, msgs)
    hist.append({"role": "user", "content": text})
    hist.append({"role": "assistant", "content": reply})
    await save_memory(uid, hist[-MEM_LIMIT:])
    await update.effective_message.reply_text(reply[:4000])

async def health(request):
    return web.Response(status=200, text="ok")

def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("reset", do_reset))
    app.add_handler(CommandHandler("read", read_url))
    app.add_handler(CommandHandler("say", say_cmd))
    app.add_handler(CallbackQueryHandler(on_button, pattern="^start$"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

async def tg_webhook(request):
    try:
        raw = await request.text()
        data = json.loads(raw)
        upd = Update.de_json(data, application.bot)
        await application.process_update(upd)
        return web.Response(status=200, text="ok")
    except Exception as e:
        print("WEBHOOK ERROR:", e, flush=True)
        return web.Response(status=200, text="ok")

async def set_menu_wrap(app: Application):
    try:
        await set_menu(app)
    except:
        pass

async def main():
    global application
    await init_db()
    application = build_app()
    await application.initialize()
    await application.start()
    aio = web.Application()
    aio.add_routes([web.get("/health", health)])
    aio.add_routes([web.post("/tgwebhook", tg_webhook)])
    runner = web.AppRunner(aio); await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT); await site.start()
    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)
    await set_menu_wrap(application)
    print("READY"); print("WEBHOOK:", f"{BASE_URL}/tgwebhook", flush=True)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
