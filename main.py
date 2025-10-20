import os, io, re, json, asyncio, tempfile, uuid, signal
from urllib.parse import urlparse
from datetime import datetime
from typing import List, Dict, Any, Optional
import httpx, asyncpg, pandas as pd
from aiohttp import web
from openai import AsyncOpenAI
from duckduckgo_search import DDGS
from readability import Document
from lxml.html.clean import Cleaner
from bs4 import BeautifulSoup
from pdfminer.high_level import extract_text as pdf_extract_text
from docx import Document as DocxDocument
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import tiktoken

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
BASE_URL = os.getenv("BASE_URL", "")
DB_URL = os.getenv("DB_URL", "")
ALWAYS_WEB = os.getenv("ALWAYS_WEB", "true").lower() == "true"
LANG = os.getenv("LANGUAGE", "ru")
MEM_LIMIT = int(os.getenv("MEMORY_LIMIT", "1500"))
VOICE_MODE = os.getenv("VOICE_MODE", "true").lower() == "true"
PORT = int(os.getenv("PORT", "8080"))

aclient = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=30)
application: Optional[Application] = None
http_timeout = 20.0
enc = tiktoken.get_encoding("cl100k_base")

ALLOWED_SCHEMES = {"http", "https"}
BLOCKED_PREFIX = ("localhost", "127.", "0.0.0.0", "10.", "192.168.", "172.")

def safe_url(url: str) -> bool:
    try:
        u = urlparse(url)
        if u.scheme not in ALLOWED_SCHEMES:
            return False
        host = u.hostname or ""
        return not host.startswith(BLOCKED_PREFIX)
    except Exception:
        return False

async def db_conn():
    return await asyncpg.connect(DB_URL)

async def init_db():
    c = await db_conn()
    await c.execute("create table if not exists users (user_id bigint primary key, lang text default 'ru', persona text default 'assistant', voice boolean default true, translate_to text default null, voicetrans boolean default false)")
    await c.execute("create table if not exists memory (user_id bigint references users(user_id) on delete cascade, role text, content text, ts timestamptz default now())")
    await c.close()

async def get_user(uid: int) -> Dict[str, Any]:
    c = await db_conn()
    row = await c.fetchrow("select user_id,lang,persona,voice,translate_to,voicetrans from users where user_id=$1", uid)
    if not row:
        await c.execute("insert into users(user_id,lang,persona,voice,translate_to,voicetrans) values($1,$2,$3,$4,$5,$6)", uid, LANG, "assistant", True, None, False)
        row = await c.fetchrow("select user_id,lang,persona,voice,translate_to,voicetrans from users where user_id=$1", uid)
    await c.close()
    d = dict(row)
    return {"user_id": d["user_id"], "lang": d["lang"], "persona": d["persona"], "voice": d["voice"], "translate_to": d["translate_to"], "voicetrans": d["voicetrans"]}

async def set_user(uid: int, **kw):
    if not kw:
        return
    fields, vals = [], []
    for k, v in kw.items():
        fields.append(f"{k}=${len(vals)+1}")
        vals.append(v)
    vals.append(uid)
    q = "update users set " + ", ".join(fields) + " where user_id=$" + str(len(vals))
    c = await db_conn()
    await c.execute(q, *vals)
    await c.close()

async def get_memory(uid: int) -> List[Dict[str, str]]:
    c = await db_conn()
    rows = await c.fetch("select role,content from memory where user_id=$1 order by ts asc", uid)
    await c.close()
    hist = [{"role": r["role"], "content": r["content"]} for r in rows]
    out, used = [], 0
    for m in reversed(hist):
        used += len(enc.encode(m["content"][:8000]))
        out.append(m)
        if used > MEM_LIMIT:
            break
    return list(reversed(out))

async def add_memory(uid: int, role: str, content: str):
    c = await db_conn()
    await c.execute("insert into memory(user_id,role,content) values($1,$2,$3)", uid, role, content)
    await c.close()

async def reset_memory(uid: int):
    c = await db_conn()
    await c.execute("delete from memory where user_id=$1", uid)
    await c.close()

def sys_prompt(persona: str, lang: str) -> str:
    if persona == "professor":
        base = "Объясняй подробно, по шагам, с примерами."
    elif persona == "sarcastic":
        base = "Отвечай с лёгкой иронией, но помогай."
    else:
        base = "Отвечай коротко и по делу."
    return f"{base} Язык ответа: {lang}. Если дан URL или просили актуальную инфу, используй веб-контент, если он приложен."

async def ddg_search(q: str, k: int = 5) -> List[Dict[str, str]]:
    out = []
    with DDGS(timeout=http_timeout) as dd:
        for r in dd.text(q, max_results=k):
            out.append({"title": r.get("title",""), "href": r.get("href",""), "body": r.get("body","")})
    return out

async def fetch_url(url: str) -> str:
    if not safe_url(url):
        return ""
    async with httpx.AsyncClient(timeout=http_timeout, follow_redirects=True, headers={"User-Agent":"JarvisBot/1.1"}) as x:
        r = await x.get(url)
        html = r.text
    doc = Document(html)
    cleaned = doc.summary()
    cleaner = Cleaner(style=True, scripts=True, comments=True, links=False, meta=False, page_structure=False, processing_instructions=True, embedded=True, frames=True, forms=True, annoying_tags=True, remove_unknown_tags=False)
    cleaned = cleaner.clean_html(cleaned)
    soup = BeautifulSoup(cleaned, "html.parser")
    text = " ".join(soup.get_text(" ").split())
    return text[:15000]

async def web_context(query: str) -> str:
    try:
        results = await ddg_search(query, 5)
        chunks = []
        for r in results[:3]:
            u = r["href"]
            if not u.startswith("http"):
                continue
            try:
                t = await fetch_url(u)
                if t:
                    chunks.append(f"{r['title']}\n{u}\n{t}\n")
            except Exception:
                continue
        return "\n\n".join(chunks)[:20000]
    except Exception:
        return ""

def guess_lang(text: str) -> str:
    return "ru" if re.search(r"[А-Яа-яЁё]", text) else "en"

async def llm(messages: List[Dict[str,str]], sys: str) -> str:
    r = await aclient.chat.completions.create(model=OPENAI_MODEL, messages=[{"role":"system","content":sys}]+messages, temperature=0.6, max_tokens=1000)
    return r.choices[0].message.content or ""

async def to_tts(text: str, voice: str = "alloy") -> Optional[bytes]:
    try:
        resp = await aclient.audio.speech.create(model="gpt-4o-mini-tts", voice=voice, input=text)
        if hasattr(resp, "content") and isinstance(resp.content, (bytes, bytearray)):
            return bytes(resp.content)
        if hasattr(resp, "read"):
            return resp.read()
    except Exception:
        return None
    return None

async def transcribe(file_path: str) -> str:
    with open(file_path, "rb") as f:
        r = await aclient.audio.transcriptions.create(model="whisper-1", file=f, language="auto")
    return getattr(r, "text", "") or ""

async def translate_text(text: str, to_lang: str) -> str:
    r = await aclient.chat.completions.create(model=OPENAI_MODEL, messages=[{"role":"system","content":f"Переведи на {to_lang} кратко и точно."},{"role":"user","content":text}], temperature=0.2, max_tokens=800)
    return r.choices[0].message.content or ""

async def summarize_text(text: str, lang: str) -> str:
    r = await aclient.chat.completions.create(model=OPENAI_MODEL, messages=[{"role":"system","content":f"Суммируй на {lang}, структурировано, по пунктам."},{"role":"user","content":text}], temperature=0.3, max_tokens=600)
    return r.choices[0].message.content or ""

async def openai_image(prompt: str) -> bytes:
    im = await aclient.images.generate(model="gpt-image-1", prompt=prompt, size="1024x1024", response_format="b64_json")
    import base64
    return base64.b64decode(im.data[0].b64_json)

async def parse_file(file_path: str, file_name: str) -> str:
    n = (file_name or "").lower()
    if n.endswith(".pdf"):
        return pdf_extract_text(file_path)[:20000]
    if n.endswith(".docx"):
        d = DocxDocument(file_path)
        return "\n".join([p.text for p in d.paragraphs])[:20000]
    if n.endswith(".csv"):
        df = pd.read_csv(file_path)
        return df.to_markdown(index=False)[:20000]
    if n.endswith(".xlsx") or n.endswith(".xls"):
        df = pd.read_excel(file_path)
        return df.to_markdown(index=False)[:20000]
    with open(file_path, "r", errors="ignore") as f:
        return f.read()[:20000]

def build_telegram_app() -> Application:
    return Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await get_user(uid)
    await update.message.reply_text("Привет! Я Jarvis. Доступно: /weather <город>, /currency <база> [символы], /news [запрос], /fact, /reset, /setlang <ru|en|...>, /personality <assistant|professor|sarcastic>, /voicetrans <on|off>, /image <промпт>, /stats.")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reset_memory(update.effective_user.id)
    await update.message.reply_text("Контекст очищен.")

async def cmd_setlang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /setlang ru")
        return
    lang = context.args[0].lower()
    await set_user(update.effective_user.id, lang=lang)
    await update.message.reply_text(f"Язык по умолчанию: {lang}")

async def cmd_personality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("assistant | professor | sarcastic")
        return
    p = context.args[0].lower()
    if p not in ["assistant","professor","sarcastic"]:
        await update.message.reply_text("assistant | professor | sarcastic")
        return
    await set_user(update.effective_user.id, persona=p)
    await update.message.reply_text(f"Персональность: {p}")

async def cmd_voicetrans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /voicetrans on|off")
        return
    on = context.args[0].lower() in ["on","1","true","yes"]
    await set_user(update.effective_user.id, voicetrans=on)
    await update.message.reply_text("Перевод voice: " + ("включён" if on else "выключен"))

async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /weather Moscow")
        return
    city = " ".join(context.args)
    try:
        u = f"https://wttr.in/{city}?format=j1"
        async with httpx.AsyncClient(timeout=http_timeout) as x:
            r = await x.get(u)
            j = r.json()
        cur = j["current_condition"][0]
        area = j["nearest_area"][0]["areaName"][0]["value"]
        msg = f"{area}: {cur['temp_C']}°C (ощущается {cur['FeelsLikeC']}°C), {cur['weatherDesc'][0]['value']}"
    except Exception:
        msg = "Не удалось получить погоду."
    await update.message.reply_text(msg)

async def cmd_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /currency usd rub,eur")
        return
    base = context.args[0].upper()
    syms = (context.args[1] if len(context.args) > 1 else "RUB,EUR").upper()
    try:
        u = f"https://api.exchangerate.host/latest?base={base}&symbols={syms}"
        async with httpx.AsyncClient(timeout=http_timeout) as x:
            r = await x.get(u)
            j = r.json().get("rates",{})
        if not j:
            raise ValueError
        msg = "\n".join([f"1 {base} = {j[k]:.4f} {k}" for k in j])
    except Exception:
        msg = "Не удалось получить курсы."
    await update.message.reply_text(msg)

async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args) if context.args else "world"
    txt = await web_context(q)
    if not txt:
        await update.message.reply_text("Новости недоступны.")
        return
    try:
        s = await summarize_text(txt[:8000], (await get_user(update.effective_user.id))["lang"])
    except Exception:
        s = txt[:4000]
    await update.message.reply_text(s)

async def cmd_fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res = await ddg_search("interesting facts today", 5)
    fact = ""
    for r in res:
        u = r["href"]
        if not u.startswith("http"):
            continue
        t = await fetch_url(u)
        if t:
            fact = await summarize_text(t[:4000], (await get_user(update.effective_user.id))["lang"])
            break
    await update.message.reply_text(fact or "Факт не найден.")

async def cmd_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /image astronaut cat in neon city")
        return
    try:
        img = await openai_image(" ".join(context.args))
        await update.message.reply_photo(photo=img, filename=f"img_{uuid.uuid4().hex}.png")
    except Exception:
        await update.message.reply_text("Не удалось сгенерировать изображение.")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    c = await db_conn()
    s = await c.fetchval("select coalesce(sum(length(content)),0) from memory where user_id=$1", uid)
    await c.close()
    await update.message.reply_text(f"Использовано ~{int(s)} символов контекста.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    text = update.message.text or ""
    lang = u["lang"] or guess_lang(text)
    hist = await get_memory(uid)
    webtxt = ""
    if ALWAYS_WEB or re.search(r"https?://|новост|news|ссылк|прочитай|итог|resume|summar", text, re.I):
        webtxt = await web_context(text)
        if webtxt:
            hist.append({"role":"system","content":"Веб-контент:\n"+webtxt})
    sys = sys_prompt(u["persona"], lang)
    reply = await llm(hist + [{"role":"user","content":text}], sys)
    await add_memory(uid, "user", text)
    await add_memory(uid, "assistant", reply)
    await update.message.reply_text(reply)

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.message.document
    f = await doc.get_file()
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        await f.download_to_drive(tmp.name)
        content = await parse_file(tmp.name, doc.file_name or "file")
    lang = (await get_user(uid))["lang"]
    s = await summarize_text(content[:18000], lang)
    await add_memory(uid, "user", "[файл]")
    await add_memory(uid, "assistant", s)
    await update.message.reply_text(s)

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    v = update.message.voice or update.message.audio
    if not v:
        await update.message.reply_text("Голос не найден.")
        return
    f = await v.get_file()
    with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp:
        await f.download_to_drive(tmp.name)
        text = await transcribe(tmp.name)
    lang = u["lang"] or guess_lang(text)
    hist = await get_memory(uid)
    if u["voicetrans"] and u["translate_to"]:
        reply = await translate_text(text, u["translate_to"])
    else:
        webtxt = ""
        if ALWAYS_WEB or re.search(r"https?://|новост|news|ссылк|прочитай|итог|resume|summar", text, re.I):
            webtxt = await web_context(text)
            if webtxt:
                hist.append({"role":"system","content":"Веб-контент:\n"+webtxt})
        sys = sys_prompt(u["persona"], lang)
        reply = await llm(hist + [{"role":"user","content":text}], sys)
    await add_memory(uid, "user", text)
    await add_memory(uid, "assistant", reply)
    if VOICE_MODE:
        audio = await to_tts(reply, "alloy")
        if audio:
            await update.message.reply_voice(voice=audio)
            return
    await update.message.reply_text(reply)

async def tg_webhook(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False}, status=400)
    try:
        upd = Update.de_json(data, application.bot)
        asyncio.create_task(application.process_update(upd))
    except Exception:
        return web.json_response({"ok": False}, status=200)
    return web.json_response({"ok": True})

async def health(request):
    return web.Response(text="ok")

def routes_app():
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/tgwebhook", tg_webhook)
    return app

def add_handlers(app: Application):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("setlang", cmd_setlang))
    app.add_handler(CommandHandler("personality", cmd_personality))
    app.add_handler(CommandHandler("voicetrans", cmd_voicetrans))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("currency", cmd_currency))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("fact", cmd_fact))
    app.add_handler(CommandHandler("image", cmd_image))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

async def start_http():
    global application
    await init_db()
    if application is None:
        application = build_telegram_app()
        add_handlers(application)
        await application.initialize()
        await application.start()
    aio = routes_app()
    runner = web.AppRunner(aio)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL.rstrip('/')}/tgwebhook", drop_pending_updates=True)
    return aio

async def main():
    await start_http()
    evt = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, evt.set)
        loop.add_signal_handler(signal.SIGTERM, evt.set)
    except NotImplementedError:
        pass
    await evt.wait()

def run():
    asyncio.run(main())

if __name__ == "__main__":
    run()
