import os, re, io, json, tempfile, asyncio
from datetime import datetime
from typing import Dict, Any, Tuple, List

import asyncpg, httpx, pandas as pd
from aiohttp import web
from bs4 import BeautifulSoup
from readability import Document as ReadabilityDoc
from duckduckgo_search import DDGS
from pdfminer.high_level import extract_text as pdf_text
from docx import Document as Docx
from openai import OpenAI

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputFile, BotCommand
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler, ContextTypes, filters
)

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
UA = "Mozilla/5.0"
VOICE_NAME = "alloy"

SYS = f"Ты Jarvis — ассистент на {LANG}. Отвечай по делу, кратко, вежливо. Если нужна свежая инфа, используй сводку из system. Если пользователь явно просит перевод, переводи без лишних комментариев."

oc = OpenAI(api_key=OPENAI_KEY)
application: Application | None = None

async def db_conn():
    return await asyncpg.connect(DB_URL)

async def init_db():
    c = await db_conn()
    await c.execute("""
        create table if not exists users(
            user_id bigint primary key,
            memory jsonb default '[]'::jsonb,
            mode text default 'concise',
            voice boolean default true,
            lang text default 'ru',
            translate_to text default 'ru'
        )
    """)
    await c.execute("alter table users alter column memory type jsonb using coalesce(memory,'[]'::jsonb)")
    await c.execute("alter table users alter column memory set default '[]'::jsonb")
    await c.execute("alter table users add column if not exists mode text default 'concise'")
    await c.execute("alter table users add column if not exists voice boolean default true")
    await c.execute("alter table users add column if not exists lang text default $1", LANG)
    await c.execute("alter table users add column if not exists translate_to text default $1", LANG)
    await c.close()

async def get_user(uid: int) -> Dict[str, Any]:
    c = await db_conn()
    r = await c.fetchrow("select user_id,memory,mode,voice,lang,translate_to from users where user_id=$1", uid)
    await c.close()
    if not r:
        return {"user_id": uid, "memory": [], "mode": "concise", "voice": True, "lang": LANG, "translate_to": LANG}
    mem = r["memory"]
    if isinstance(mem, str):
        try:
            mem = json.loads(mem) if mem else []
        except:
            mem = []
    return {
        "user_id": r["user_id"],
        "memory": mem or [],
        "mode": r["mode"] or "concise",
        "voice": bool(r["voice"]),
        "lang": r["lang"] or LANG,
        "translate_to": r["translate_to"] or LANG,
    }

async def save_user(uid: int, mem: List[Dict[str, str]], mode: str, voice: bool, lang: str, tr_to: str):
    c = await db_conn()
    await c.execute(
        """insert into users(user_id,memory,mode,voice,lang,translate_to)
           values($1,$2,$3,$4,$5,$6)
           on conflict(user_id) do update set
           memory=excluded.memory,
           mode=excluded.mode,
           voice=excluded.voice,
           lang=excluded.lang,
           translate_to=excluded.translate_to""",
        uid, json.dumps(mem), mode, voice, lang, tr_to
    )
    await c.close()

async def save_memory(uid: int, mem: List[Dict[str, str]]):
    u = await get_user(uid)
    await save_user(uid, mem, u["mode"], u["voice"], u["lang"], u["translate_to"])

def sentiment_tag(text: str) -> str:
    t = text.lower()
    score = 0
    if any(w in t for w in ["устал", "плохо", "груст", "тяжело", "тревог", "стресс", "вымот"]): score -= 2
    if any(w in t for w in ["нрав", "рад", "класс", "отлич", "супер", "круто", "огонь"]): score += 2
    if "?" in t and any(w in t for w in ["не знаю", "как", "зачем"]): score -= 1
    if score <= -2: return "low"
    if score >= 2: return "high"
    return "mid"

def empathy_reply(text: str, mood: str, mode: str) -> str:
    if mood == "low":
        return "Понимаю. Давай сделаем паузу и разгрузим голову. Хочешь — предложу короткий план или подбодрю цитатой."
    if mood == "high":
        return "Отлично звучит! Держим темп. Готов помочь следующей задачей."
    return "Окей. Я здесь, чтобы помочь. Сформулируй цель — и мы быстро её разложим на шаги."

def ask_openai(messages, temperature=0.3, max_tokens=800) -> str:
    r = oc.chat.completions.create(model=MODEL, messages=messages, temperature=temperature, max_tokens=max_tokens)
    return r.choices[0].message.content.strip()

async def fetch_url(url: str, limit=20000) -> str:
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": UA}, timeout=25) as cl:
        r = await cl.get(url)
    ct = (r.headers.get("content-type") or "").lower()
    if "text/html" in ct or "<html" in r.text[:500].lower():
        html = ReadabilityDoc(r.text).summary()
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n", strip=True)
    else:
        text = r.text
    return re.sub(r"\n{3,}", "\n\n", text)[:limit]

def need_web(q: str) -> bool:
    t = q.lower()
    keys = ["сейчас","сегодня","новост","курс","цена","сколько стоит","когда будет","последн","обнов","релиз","погода","расписан","матч","акции","доступно","вышел","итог"]
    if any(k in t for k in keys): return True
    if re.search(r"\b20(2[4-9]|3\d)\b", t): return True
    if "http://" in t or "https://" in t: return True
    return False

def extract_urls(q: str) -> List[str]:
    return re.findall(r"https?://\S+", q)

async def fetch_urls(urls: List[str], limit_chars=12000) -> str:
    out = []
    for u in urls[:3]:
        try:
            t = await fetch_url(u, limit=4000)
            if t: out.append(t)
        except:
            pass
    return "\n\n".join(out)[:limit_chars]

async def search_and_fetch(query: str, hits: int = 2, limit_chars: int = 12000) -> str:
    links = []
    try:
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=hits, safesearch="moderate"):
                if r and r.get("href"):
                    links.append(r["href"])
    except:
        pass
    return await fetch_urls(links, limit_chars) if links else ""

def read_txt(p): 
    return open(p, "r", encoding="utf-8", errors="ignore").read()

def read_pdf(p):
    return pdf_text(p) or ""

def read_docx(p):
    d = Docx(p)
    return "\n".join([x.text for x in d.paragraphs])

def read_table(p):
    if p.lower().endswith((".xlsx",".xls")):
        df = pd.read_excel(p)
    else:
        df = pd.read_csv(p)
    b = io.StringIO()
    df.head(80).to_string(b)
    return b.getvalue()

def read_any(p):
    pl = p.lower()
    if pl.endswith((".txt",".md",".log")): return read_txt(p)
    if pl.endswith(".pdf"): return read_pdf(p)
    if pl.endswith(".docx"): return read_docx(p)
    if pl.endswith((".csv",".xlsx",".xls")): return read_table(p)
    return read_txt(p)

def transcribe(path: str) -> str:
    with open(path, "rb") as f:
        r = oc.audio.transcriptions.create(model="whisper-1", file=f)
    return (r.text or "").strip()

def tts_to_mp3(text: str) -> str:
    fn = tempfile.mktemp(suffix=".mp3")
    with oc.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts",
        voice=VOICE_NAME,
        input=text
    ) as resp:
        resp.stream_to_file(fn)
    return fn

def parse_translate_pref(text: str) -> Tuple[bool, str, str]:
    m = re.match(r"^\s*tr->([a-z]{2})\s*:\s*(.+)$", text.strip(), re.I)
    if m:
        return True, m.group(1).lower(), m.group(2).strip()
    return False, "", text.strip()

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await set_menu(ctx.application)
    await ensure_user(update.effective_user.id)
    await update.message.reply_text("Привет, я Jarvis v2.2 Ultimate 🤖", reply_markup=main_menu())

async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    await save_user(uid, [], u["mode"], u["voice"], u["lang"], u["translate_to"])
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
        return await update.message.reply_text("Голос отключен")
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await update.message.reply_text("Формат: /say текст")
    mp3 = tts_to_mp3(parts[1].strip())
    try:
        with open(mp3, "rb") as f:
            await update.message.reply_audio(InputFile(f, filename="jarvis.mp3"))
    finally:
        try: os.remove(mp3)
        except: pass

def main_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("☀️ Погода", callback_data="menu_weather"),
         InlineKeyboardButton("💸 Курс", callback_data="menu_currency")],
        [InlineKeyboardButton("🌍 Новости", callback_data="menu_news"),
         InlineKeyboardButton("🧠 Факт", callback_data="menu_fact")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")]
    ]
    return InlineKeyboardMarkup(rows)

def settings_menu(u: Dict[str, Any]) -> InlineKeyboardMarkup:
    voice = "🔊Вкл" if u["voice"] else "🔇Выкл"
    mode = "Кратко" if u["mode"] == "concise" else "Развернуто"
    rows = [
        [InlineKeyboardButton(f"Язык: {u['lang']}", callback_data="set_lang"),
         InlineKeyboardButton(f"Озвучка: {voice}", callback_data="toggle_voice")],
        [InlineKeyboardButton(f"Стиль: {mode}", callback_data="toggle_mode"),
         InlineKeyboardButton(f"Перевод в: {u['translate_to']}", callback_data="set_tr")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")]
    ]
    return InlineKeyboardMarkup(rows)

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    u = await get_user(uid)
    data = q.data or ""
    if data == "menu_back":
        await q.edit_message_text("Главное меню:", reply_markup=main_menu())
        return
    if data == "menu_weather":
        await q.edit_message_text("Напиши: /weather Город  (пример: /weather Москва)")
        return
    if data == "menu_currency":
        await q.edit_message_text("Напиши: /currency usd  или /currency eur")
        return
    if data == "menu_news":
        await q.edit_message_text("Напиши: /news Тема  (или просто /news)")
        return
    if data == "menu_fact":
        fact = await random_fact()
        await q.edit_message_text(f"Факт: {fact}")
        return
    if data == "menu_settings":
        await q.edit_message_text("Настройки:", reply_markup=settings_menu(u))
        return
    if data == "toggle_voice":
        u["voice"] = not u["voice"]
        await save_user(uid, u["memory"], u["mode"], u["voice"], u["lang"], u["translate_to"])
        await q.edit_message_text("Настройки:", reply_markup=settings_menu(u))
        return
    if data == "toggle_mode":
        u["mode"] = "verbose" if u["mode"] == "concise" else "concise"
        await save_user(uid, u["memory"], u["mode"], u["voice"], u["lang"], u["translate_to"])
        await q.edit_message_text("Настройки:", reply_markup=settings_menu(u))
        return
    if data == "set_lang":
        await q.edit_message_text("Отправь: /setlang ru  или  /setlang en")
        return
    if data == "set_tr":
        await q.edit_message_text("Отправь: /settr en  (язык для перевода по умолчанию)")
        return
    await q.answer()

async def random_fact() -> str:
    prompt = [{"role": "system", "content": "Сгенерируй один любопытный факт на русском, 1-2 предложения."},
              {"role": "user", "content": "Дай один случайный факт."}]
    try:
        return ask_openai(prompt, temperature=0.8, max_tokens=120)
    except:
        return "Иногда даже короткий отдых повышает продуктивность."

async def cmd_weather(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    city = parts[1] if len(parts) > 1 else "Moscow"
    url = f"https://wttr.in/{city}?format=3"
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as cl:
            r = await cl.get(url)
        await update.message.reply_text(r.text.strip()[:4000])
    except Exception as e:
        await update.message.reply_text(f"Ошибка погоды: {e}")

async def cmd_currency(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    base = (parts[1].strip().upper() if len(parts) > 1 else "USD")[:3]
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            r = await cl.get(f"https://api.exchangerate.host/latest?base={base}")
        data = r.json()
        eur = data["rates"].get("EUR")
        rub = data["rates"].get("RUB")
        msg = f"{base} → EUR: {eur:.4f}, RUB: {rub:.2f}" if eur and rub else f"Курсы для {base} недоступны"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Ошибка курса: {e}")

async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = ((update.message.text or "").split(maxsplit=1)[1] if len((update.message.text or '').split()) > 1 else "главные новости")
    try:
        summary = await search_and_fetch(query, hits=3)
        if not summary:
            return await update.message.reply_text("Не нашел свежего.")
        out = ask_openai(
            [{"role": "system", "content": "Суммаризируй кратко 5 пунктами."},
             {"role": "user", "content": summary[:14000]}],
            temperature=0.2, max_tokens=400
        )
        await update.message.reply_text(out[:4000])
    except Exception as e:
        await update.message.reply_text(f"Ошибка новостей: {e}")

async def cmd_setlang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await update.message.reply_text("Формат: /setlang ru")
    lang = parts[1].strip().lower()[:5]
    u = await get_user(update.effective_user.id)
    u["lang"] = lang
    await save_user(u["user_id"], u["memory"], u["mode"], u["voice"], u["lang"], u["translate_to"])
    await update.message.reply_text(f"Язык интерфейса: {lang}")

async def cmd_settr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await update.message.reply_text("Формат: /settr en")
    tr = parts[1].strip().lower()[:5]
    u = await get_user(update.effective_user.id)
    u["translate_to"] = tr
    await save_user(u["user_id"], u["memory"], u["mode"], u["voice"], u["lang"], u["translate_to"])
    await update.message.reply_text(f"Язык перевода по умолчанию: {tr}")

async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not VOICE_MODE: 
        return
    v = update.message.voice or update.message.audio
    if not v: 
        return
    f = await ctx.bot.get_file(v.file_id)
    p = await f.download_to_drive()
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, transcribe, p)
    if not text:
        return await update.message.reply_text("Не удалось распознать голос.")
    uid = update.effective_user.id
    u = await get_user(uid)
    hist = u["memory"]
    is_tr, lang_to, clean = parse_translate_pref(text)
    if is_tr:
        msgs = [{"role":"system","content":"Ты переводчик. Переведи текст на целевой язык без пояснений."},
                {"role":"user","content":f"Целевой язык: {lang_to}\nТекст: {clean}"}]
        reply = ask_openai(msgs, temperature=0.2, max_tokens=800)
    else:
        urls = extract_urls(clean)
        web_snip = ""
        if urls:
            try: web_snip = await fetch_urls(urls)
            except: web_snip = ""
        elif need_web(clean):
            try: web_snip = await search_and_fetch(clean, hits=2)
            except: web_snip = ""
        msgs = [{"role":"system","content":SYS}]
        if web_snip:
            msgs.append({"role":"system","content":"Актуальная сводка из интернета:\n"+web_snip})
        msgs += hist + [{"role":"user","content":clean}]
        reply = ask_openai(msgs, temperature=0.3, max_tokens=800)
    mood = sentiment_tag(clean)
    em = empathy_reply(clean, mood, u["mode"])
    full = f"{em}\n\n{reply}" if em and em != reply else reply
    hist.append({"role":"user","content":clean})
    hist.append({"role":"assistant","content":full})
    await save_memory(uid, hist[-MEM_LIMIT:])
    if VOICE_MODE and u["voice"]:
        mp3 = tts_to_mp3(full)
        try:
            with open(mp3,"rb") as f:
                await update.message.reply_audio(InputFile(f, filename="jarvis.mp3"))
        finally:
            try: os.remove(mp3)
            except: pass
    else:
        await update.message.reply_text(full[:4000])

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    t = (update.message.text or update.message.caption or "").strip()
    if not t:
        return
    u = await get_user(uid)
    hist = u["memory"]
    is_tr, lang_to, clean = parse_translate_pref(t)
    if is_tr:
        msgs = [{"role":"system","content":"Ты переводчик. Переведи текст на целевой язык без пояснений."},
                {"role":"user","content":f"Целевой язык: {lang_to}\nТекст: {clean}"}]
        reply = ask_openai(msgs, temperature=0.2, max_tokens=800)
        hist.append({"role":"user","content":t})
        hist.append({"role":"assistant","content":reply})
        await save_memory(uid, hist[-MEM_LIMIT:])
        await update.message.reply_text(reply[:4000])
        return
    urls = extract_urls(clean)
    web_snip = ""
    if urls:
        try: web_snip = await fetch_urls(urls)
        except: web_snip = ""
    elif need_web(clean):
        try: web_snip = await search_and_fetch(clean, hits=2)
        except: web_snip = ""
    msgs = [{"role":"system","content":SYS}]
    if web_snip:
        msgs.append({"role":"system","content":"Актуальная сводка из интернета:\n"+web_snip})
    msgs += hist + [{"role":"user","content":clean}]
    reply = ask_openai(msgs, temperature=0.3, max_tokens=800)
    mood = sentiment_tag(clean)
    em = empathy_reply(clean, mood, u["mode"])
    full = f"{em}\n\n{reply}" if em and em != reply else reply
    hist.append({"role":"user","content":clean})
    hist.append({"role":"assistant","content":full})
    await save_memory(uid, hist[-MEM_LIMIT:])
    await update.message.reply_text(full[:4000])

async def cmd_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        return await update.message.reply_text("Пришли файл.")
    f = await ctx.bot.get_file(doc.file_id)
    p = await f.download_to_drive()
    try:
        raw = read_any(p)[:16000]
        sys = [{"role":"system","content":"Кратко суммаризируй и выдели тезисы."}]
        out = ask_openai(sys+[{"role":"user","content":raw}])
        await update.message.reply_text(out[:4000])
    except Exception as e:
        await update.message.reply_text(f"Ошибка чтения: {e}")

async def ensure_user(uid: int):
    u = await get_user(uid)
    await save_user(u["user_id"], u["memory"], u["mode"], u["voice"], u["lang"], u["translate_to"])

async def set_menu(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start","запуск"),
        BotCommand("ping","проверка"),
        BotCommand("read","прочитать сайт"),
        BotCommand("say","озвучить текст"),
        BotCommand("reset","сбросить память"),
        BotCommand("weather","погода"),
        BotCommand("currency","курс валют"),
        BotCommand("news","новости"),
        BotCommand("setlang","язык интерфейса"),
        BotCommand("settr","язык перевода"),
        BotCommand("upload","суммаризировать файл"),
    ])

async def health(request):
    return web.Response(text="ok")

async def migrate(request):
    if request.rel_url.query.get("key") != os.getenv("MIGRATION_KEY",""):
        return web.Response(status=403, text="forbidden")
    c = await db_conn()
    try:
        await c.execute("BEGIN")
        await c.execute("update users set memory='[]' where memory is null or memory::text='' or not (memory is json)")
        await c.execute("alter table users alter column memory type jsonb using coalesce(nullif(memory::text,''),'[]')::jsonb")
        await c.execute("alter table users alter column memory set default '[]'::jsonb")
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

def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("read", cmd_read))
    app.add_handler(CommandHandler("say", cmd_say))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("currency", cmd_currency))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("setlang", cmd_setlang))
    app.add_handler(CommandHandler("settr", cmd_settr))
    app.add_handler(CommandHandler("upload", cmd_upload))
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
    aio.add_routes([web.get("/migrate", migrate)])
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
