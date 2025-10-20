import os
import io
import re
import json
import asyncio
import tempfile
import uuid
import signal
from pathlib import Path
from urllib.parse import urlparse
import httpx
import asyncpg
import tiktoken
from aiohttp import web
from datetime import datetime
from typing import List, Dict, Any, Optional
from openai import AsyncOpenAI
import pandas as pd
from duckduckgo_search import DDGS
from readability import Document
from lxml.html.clean import Cleaner
from bs4 import BeautifulSoup
from pdfminer.high_level import extract_text as pdf_extract_text
from docx import Document as DocxDocument
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

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

aclient = AsyncOpenAI(api_key=OPENAI_API_KEY, max_retries=3, timeout=30)
application: Optional[Application] = None
http_timeout = 20.0
enc = tiktoken.get_encoding("cl100k_base")

ALLOWED_SCHEMES = {"http", "https"}
BLOCKED_NETLOCS_PREFIX = ("localhost", "127.", "0.0.0.0", "10.", "192.168.", "172.")

def safe_url(url: str) -> bool:
    try:
        u = urlparse(url)
        if u.scheme not in ALLOWED_SCHEMES:
            return False
        host = (u.hostname or "").lower()
        return not host.startswith(BLOCKED_NETLOCS_PREFIX)
    except Exception:
        return False

async def db_conn():
    return await asyncpg.connect(DB_URL)

async def init_db():
    c = await db_conn()
    await c.execute(f"create table if not exists users (user_id bigint primary key, lang text default '{LANG}', persona text default 'assistant', voice boolean default true, translate_to text default null, voicetrans boolean default false)")
    await c.execute("create table if not exists memory (user_id bigint references users(user_id) on delete cascade, role text, content text, ts timestamptz default now())")
    await c.close()

async def get_user(uid: int) -> dict:
    c = await db_conn()
    row = await c.fetchrow("select user_id,lang,persona,voice,translate_to,voicetrans from users where user_id=$1", uid)
    if not row:
        await c.execute("insert into users(user_id) values($1)", uid)
        row = await c.fetchrow("select user_id,lang,persona,voice,translate_to,voicetrans from users where user_id=$1", uid)
    await c.close()
    d = dict(row)
    return {"user_id": d["user_id"], "lang": d["lang"], "persona": d["persona"], "voice": d["voice"], "translate_to": d["translate_to"], "voicetrans": d["voicetrans"]}

async def set_user(uid: int, **kw):
    if not kw:
        return
    fields = []
    vals = []
    for k, v in kw.items():
        fields.append(f"{k}=${len(vals)+1}")
        vals.append(v)
    vals.append(uid)
    q = "update users set " + ", ".join(fields) + " where user_id=$" + str(len(vals))
    c = await db_conn()
    await c.execute(q, *vals)
    await c.close()
    
def token_len(s: str) -> int:
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model(OPENAI_MODEL)
        return len(enc.encode(s))
    except Exception:
        return max(1, len(s) // 4)
        
async def get_memory(uid: int) -> List[Dict[str, str]]:
    c = await db_conn()
    rows = await c.fetch("select role,content from memory where user_id=$1 order by ts asc", uid)
    await c.close()
    hist = [{"role": r["role"], "content": r["content"]} for r in rows]
    s = 0
    out = []
    for m in reversed(hist):
        s += token_len(m["content"])
        out.append(m)
        if s > MEM_LIMIT:
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
    base = "Отвечай кратко и по делу."
    if persona == "professor":
        base = "Объясняй подробно, по шагам, приводя примеры и уточнения."
    if persona == "sarcastic":
        base = "Отвечай с лёгкой ироничностью, но оставайся полезным и доброжелательным."
    return f"{base} Язык ответа: {lang}. Если пользователь просит другой язык — следуй ему. Если дан URL или вопрос о текущих событиях — используй нижеприведённый веб-контент."

async def empathize(text: str, lang: str) -> str:
    try:
        r = await aclient.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content": "Определи настроение пользователя: neutral, positive, stressed, sad, angry. Верни одно слово."}, {"role": "user", "content": text}],
            temperature=0.2,
            max_tokens=5,
        )
        mood = (r.choices[0].message.content or "neutral").strip().lower()
    except Exception:
        mood = "neutral"
    if lang.startswith("ru"):
        d = {"positive": "Рад это слышать!", "stressed": "Понимаю. Давай разгрузим голову — я рядом.", "sad": "Сочувствую. Готов поддержать.", "angry": "Понимаю злость. Постараюсь помочь конструктивно.", "neutral": "Принято."}
    else:
        d = {"positive": "Glad to hear!", "stressed": "I get it. I'm here to help.", "sad": "Sorry to hear that.", "angry": "I understand. Let's fix it.", "neutral": "Got it."}
    return d.get(mood, "Got it.")

async def ddg_search(q: str, k: int = 5) -> List[Dict[str, str]]:
    out = []
    with DDGS(timeout=10) as dd:
        for r in dd.text(q, max_results=k):
            out.append({"title": r.get("title", ""), "href": r.get("href", ""), "body": r.get("body", "")})
    return out

async def fetch_url(url: str) -> str:
    if not safe_url(url):
        return ""
    async with httpx.AsyncClient(timeout=http_timeout, follow_redirects=True, headers={"User-Agent": "JarvisBot/1.0"}) as x:
        r = await x.get(url)
        html = r.text
    doc = Document(html)
    cleaned = doc.summary()
    cleaner = Cleaner(style=True, scripts=True, comments=True, links=False, meta=False, page_structure=False, processing_instructions=True, embedded=True, frames=True, forms=True, annoying_tags=True, remove_unknown_tags=False)
    cleaned = cleaner.clean_html(cleaned)
    soup = BeautifulSoup(cleaned, "html.parser")
    text = " ".join(soup.get_text(" ").split())
    return text[:12000]

async def web_context(query: str) -> str:
    try:
        results = await ddg_search(query, 5)
        chunks = []
        for r in results[:3]:
            u = r["href"]
            if not u or not u.startswith("http"):
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

async def weather(city: str) -> str:
    u = f"https://wttr.in/{city}?format=j1"
    async with httpx.AsyncClient(timeout=http_timeout) as x:
        r = await x.get(u)
        j = r.json()
    cur = j["current_condition"][0]
    area = j["nearest_area"][0]["areaName"][0]["value"]
    temp = cur["temp_C"]
    feels = cur["FeelsLikeC"]
    w = cur["weatherDesc"][0]["value"]
    return f"{area}: {temp}°C (ощущается {feels}°C), {w}"

async def currency(base: str = "USD", symbols: str = "RUB,EUR") -> str:
    u = f"https://api.exchangerate.host/latest?base={base.upper()}&symbols={symbols.upper()}"
    async with httpx.AsyncClient(timeout=http_timeout) as x:
        r = await x.get(u)
        j = r.json()
    rates = j.get("rates", {})
    items = [f"1 {base.upper()} = {rates[k]:.4f} {k}" for k in rates]
    return "\n".join(items) if items else "N/A"

async def latest_news(q: str = "world") -> str:
    res = await ddg_search(q, 6)
    picks = []
    for r in res[:5]:
        url = r["href"]
        if not url.startswith("http"):
            continue
        try:
            txt = await fetch_url(url)
        except Exception:
            continue
        picks.append({"title": r["title"], "url": url, "text": txt[:3000]})
    if not picks:
        return "Нет новостей."
    body = "\n\n".join([f"{i+1}. {p['title']}\n{p['url']}" for i, p in enumerate(picks)])
    try:
        s = await aclient.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content": "Суммируй пункты кратко списком."}, {"role": "user", "content": body}],
            temperature=0.3,
            max_tokens=500,
        )
        summ = s.choices[0].message.content
    except Exception:
        summ = body
    return summ

async def random_fact() -> str:
    res = await ddg_search("interesting facts today", 5)
    for r in res:
        url = r["href"]
        if not url.startswith("http"):
            continue
        try:
            t = await fetch_url(url)
        except Exception:
            continue
        try:
            s = await aclient.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "system", "content": "Выдели один интересный факт из текста, одной фразой."}, {"role": "user", "content": t[:8000]}],
                temperature=0.7,
                max_tokens=120,
            )
            return s.choices[0].message.content
        except Exception:
            continue
    return "Факт не найден."

def guess_lang(text: str) -> str:
    return "ru" if re.search(r"[А-Яа-яЁё]", text) else "en"

async def llm(messages: List[Dict[str, str]], sys: str) -> str:
    r = await aclient.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "system", "content": sys}] + messages,
        temperature=0.6,
        max_tokens=1000,
    )
    return r.choices[0].message.content

async def to_tts(text: str, voice: str = "alloy") -> bytes:
    try:
        resp = await aclient.audio.speech.create(model="gpt-4o-mini-tts", voice=voice, input=text)
        if hasattr(resp, "content"):
            return resp.content
        if hasattr(resp, "read"):
            return resp.read()
        return bytes(resp)
    except Exception:
        return b""

async def transcribe(file_path: str) -> str:
    with open(file_path, "rb") as f:
        r = await aclient.audio.transcriptions.create(model="whisper-1", file=f, language="auto")
    return getattr(r, "text", "") or ""

async def translate_text(text: str, to_lang: str) -> str:
    r = await aclient.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "system", "content": f"Переведи текст на {to_lang}. Сохраняй смысл и тон."}, {"role": "user", "content": text}],
        temperature=0.2,
        max_tokens=1000,
    )
    return r.choices[0].message.content

async def summarize_text(text: str, lang: str) -> str:
    r = await aclient.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "system", "content": f"Суммируй кратко на {lang}."}, {"role": "user", "content": text}],
        temperature=0.3,
        max_tokens=600,
    )
    return r.choices[0].message.content

async def openai_image(prompt: str) -> bytes:
    im = await aclient.images.generate(model="gpt-image-1", prompt=prompt, size="1024x1024", response_format="b64_json")
    import base64
    return base64.b64decode(im.data[0].b64_json)

async def parse_file(file_path: str, file_name: str) -> str:
    n = file_name.lower()
    if n.endswith(".pdf"):
        return pdf_extract_text(file_path)[:20000]
    if n.endswith(".docx"):
        d = DocxDocument(file_path)
        return "\n".join([p.text for p in d.paragraphs])[:20000]
    if n.endswith(".csv"):
        df = pd.read_csv(file_path)
        return df.to_markdown()[:20000]
    if n.endswith(".xlsx") or n.endswith(".xls"):
        df = pd.read_excel(file_path)
        return df.to_markdown()[:20000]
    with open(file_path, "r", errors="ignore") as f:
        return f.read()[:20000]

def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()
    add_handlers(app)
    return app

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await get_user(uid)
    txt = "Привет! Я Jarvis. Доступно: /weather <город>, /currency <база> [символы], /news [запрос], /fact, /reset, /setlang <ru|en|...>, /personality <assistant|professor|sarcastic>, /voicetrans <on|off>, /image <промпт>. Пиши или пришли голос."
    await update.message.reply_text(txt)

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await reset_memory(uid)
    await update.message.reply_text("Окей, контекст очищен.")

async def cmd_setlang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Пример: /setlang ru")
        return
    lang = context.args[0].lower()
    await set_user(uid, lang=lang)
    await update.message.reply_text(f"Язык по умолчанию: {lang}")

async def cmd_personality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Варианты: assistant, professor, sarcastic")
        return
    p = context.args[0].lower()
    if p not in ["assistant", "professor", "sarcastic"]:
        await update.message.reply_text("Неверно. Варианты: assistant, professor, sarcastic")
        return
    await set_user(uid, persona=p)
    await update.message.reply_text(f"Персональность: {p}")

async def cmd_voicetrans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Использование: /voicetrans on|off")
        return
    on = context.args[0].lower() in ["on", "1", "true", "yes"]
    await set_user(uid, voicetrans=on)
    await update.message.reply_text("Перевод voice: " + ("включён" if on else "выключен"))

async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /weather Moscow")
        return
    city = " ".join(context.args)
    try:
        w = await weather(city)
        await update.message.reply_text(w)
    except Exception:
        await update.message.reply_text("Не удалось получить погоду.")

async def cmd_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /currency usd rub,eur")
        return
    base = context.args[0]
    syms = context.args[1] if len(context.args) > 1 else "RUB,EUR"
    try:
        r = await currency(base, syms.upper())
        await update.message.reply_text(r)
    except Exception:
        await update.message.reply_text("Не удалось получить курсы.")

async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args) if context.args else "world news today"
    try:
        s = await latest_news(q)
        await update.message.reply_text(s)
    except Exception:
        await update.message.reply_text("Новости недоступны.")

async def cmd_fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        f = await random_fact()
        await update.message.reply_text(f)
    except Exception:
        await update.message.reply_text("Факт недоступен.")

async def cmd_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /image astronaut cat in neon city")
        return
    prompt = " ".join(context.args)
    try:
        img = await openai_image(prompt)
        fn = f"image_{uuid.uuid4().hex}.png"
        await update.message.reply_photo(photo=img, filename=fn)
    except Exception:
        await update.message.reply_text("Не удалось сгенерировать изображение.")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    c = await db_conn()
    rows = await c.fetch("select sum(length(content)) as s from memory where user_id=$1", uid)
    await c.close()
    used = (rows[0]["s"] or 0) if rows else 0
    await update.message.reply_text(f"📊 Вы использовали ~{used} символов памяти.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    text = update.message.text or ""
    lang = u["lang"] or guess_lang(text)
    mood = await empathize(text, lang)
    hist = await get_memory(uid)
    if ALWAYS_WEB or re.search(r"https?://|новост|news|ссылк|прочитай|итог|resume|summar", text, re.I):
        webtxt = await web_context(text)
        if webtxt:
            hist.append({"role": "system", "content": "Веб-контент:\n" + webtxt})
    sys = sys_prompt(u["persona"], lang)
    hist2 = hist + [{"role": "user", "content": text}]
    try:
        reply = await llm(hist2, sys)
    except Exception:
        reply = "Проблема с моделью."
    await add_memory(uid, "user", text)
    await add_memory(uid, "assistant", reply)
    await update.message.reply_text(mood + "\n\n" + reply)

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    doc = update.message.document
    f = await doc.get_file()
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        await f.download_to_drive(tmp.name)
        content = await parse_file(tmp.name, doc.file_name or "file")
    lang = u["lang"]
    s = await summarize_text(content[:18000], lang)
    await add_memory(uid, "user", "[файл загружен]")
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
        try:
            text = await transcribe(tmp.name)
        except Exception:
            await update.message.reply_text("Не удалось распознать голос.")
            return
    lang = u["lang"] or guess_lang(text)
    hist = await get_memory(uid)
    if u["voicetrans"] and u["translate_to"]:
        try:
            reply = await translate_text(text, u["translate_to"])
        except Exception:
            reply = "Не удалось перевести."
    else:
        webtxt = ""
        if ALWAYS_WEB or re.search(r"https?://|новост|news|ссылк|прочитай|итог|resume|summar", text, re.I):
            webtxt = await web_context(text)
            if webtxt:
                hist.append({"role": "system", "content": "Веб-контент:\n" + webtxt})
        sys = sys_prompt(u["persona"], lang)
        hist2 = hist + [{"role": "user", "content": text}]
        try:
            reply = await llm(hist2, sys)
        except Exception:
            reply = "Проблема с моделью."
    await add_memory(uid, "user", text)
    await add_memory(uid, "assistant", reply)
    if VOICE_MODE:
        audio = await to_tts(reply, "alloy")
        if audio:
            await update.message.reply_voice(voice=audio)
        else:
            await update.message.reply_text(reply)
    else:
        await update.message.reply_text(reply)

async def tg_webhook(request):
    global application
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="bad json", status=400)
    try:
        upd = Update.de_json(data, application.bot)
        asyncio.create_task(application.process_update(upd))
    except Exception:
        return web.Response(text="error", status=200)
    return web.Response(text="ok")

async def health(request):
    return web.Response(text="ok")

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
    application = build_app()
    await application.initialize()
    await application.start()
    aio_app = web.Application()
    aio_app.router.add_get("/health", health)
    aio_app.router.add_post("/tgwebhook", tg_webhook)
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL.rstrip('/')}/tgwebhook", drop_pending_updates=True)
    return aio_app

async def main():
    await start_http()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    except NotImplementedError:
        pass
    await stop.wait()

def run():
    asyncio.run(main())

if __name__ == "__main__":
    run()
