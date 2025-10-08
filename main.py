import os, io, json, asyncio, tempfile, logging, aiohttp, asyncpg, re
from datetime import datetime
from duckduckgo_search import DDGS
from openai import AsyncOpenAI, OpenAI
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from pdfminer.high_level import extract_text as pdf_text
import pandas as pd
from docx import Document as Docx
from bs4 import BeautifulSoup
from readability import Document as Readability

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_KEY")
DB_URL = os.getenv("DB_URL", "")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
VOICE_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "alloy")
MEM_LIMIT = int(os.getenv("MEMORY_LIMIT", "1500"))
PORT = int(os.getenv("PORT", "10000"))
BASE_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_URL = f"{BASE_URL}/tgwebhook" if BASE_URL else ""

oc = AsyncOpenAI(api_key=OPENAI_KEY)
oc_sync = OpenAI(api_key=OPENAI_KEY)

UA = "Mozilla/5.0"
KEYS_WEB = ["сейчас","сегодня","новост","курс","цена","сколько стоит","когда будет","последн","обнов","релиз","погода","расписан","матч","акции","доступно","вышел","итог","breaking","price","release","today","now","score","weather","schedule","update","news"]

async def db_conn():
    return await asyncpg.connect(DB_URL)

async def init_db():
    async with await db_conn() as c:
        await c.execute("""
        create table if not exists users(
            user_id bigint primary key,
            memory jsonb default '[]'::jsonb,
            mode text default 'default',
            voice boolean default true,
            lang text default 'ru',
            translate_to text default 'en',
            created_at timestamptz default now(),
            updated_at timestamptz default now()
        )""")

def norm_memory(v):
    if v is None: return []
    if isinstance(v, (list, dict)): return v
    try:
        return json.loads(v) if isinstance(v, str) and v else []
    except:
        return []

async def get_user(uid:int):
    async with await db_conn() as c:
        row = await c.fetchrow("select user_id,memory,mode,voice,lang,translate_to from users where user_id=$1", uid)
        if row:
            mem = norm_memory(row["memory"])
            mode = row["mode"] or "default"
            voice = row["voice"] if row["voice"] is not None else True
            lang = row["lang"] or "ru"
            tr = row["translate_to"] or "en"
            return {"user_id":uid,"memory":mem,"mode":mode,"voice":voice,"lang":lang,"translate_to":tr}
        await c.execute("insert into users(user_id) values($1)", uid)
        return {"user_id":uid,"memory":[],"mode":"default","voice":True,"lang":"ru","translate_to":"en"}

async def save_memory(uid:int, mem):
    async with await db_conn() as c:
        await c.execute("update users set memory=$1,updated_at=now() where user_id=$2", asyncpg.Json(mem), uid)

async def save_user(uid:int, mem, mode:str, voice:bool, lang:str, tr:str):
    async with await db_conn() as c:
        await c.execute("""
        insert into users(user_id,memory,mode,voice,lang,translate_to,updated_at)
        values($1,$2,$3,$4,$5,$6,now())
        on conflict(user_id) do update set memory=excluded.memory,mode=excluded.mode,voice=excluded.voice,lang=excluded.lang,translate_to=excluded.translate_to,updated_at=excluded.updated_at
        """, uid, asyncpg.Json(mem), mode, voice, lang, tr)

async def chat_completion(messages, temperature=0.7, lang="ru"):
    try:
        r = await oc.chat.completions.create(model=MODEL, messages=messages, temperature=temperature)
        return r.choices[0].message.content.strip()
    except Exception as e:
        return f"⚠️ Ошибка модели: {e}"

def tts_to_file_sync(text:str)->str:
    p = tempfile.mktemp(suffix=".mp3")
    with oc_sync.audio.speech.with_streaming_response.create(model=VOICE_MODEL, voice=TTS_VOICE, input=text) as resp:
        resp.stream_to_file(p)
    return p

def need_web(q:str):
    t=q.lower()
    if any(k in t for k in KEYS_WEB): return True
    if re.search(r"\b20(2[4-9]|3\d)\b", t): return True
    if "http://" in t or "https://" in t: return True
    return False

def extract_urls(q:str):
    return re.findall(r"https?://\S+", q)

async def fetch_url(url:str, limit=20000):
    try:
        async with aiohttp.ClientSession(headers={"User-Agent":UA}) as s:
            async with s.get(url, timeout=25, allow_redirects=True) as r:
                txt = await r.text(errors="ignore")
                ct = (r.headers.get("content-type") or "").lower()
        if "text/html" in ct or "<html" in txt[:500].lower():
            html = Readability(txt).summary()
            soup = BeautifulSoup(html,"lxml")
            text = soup.get_text("\n", strip=True)
        else:
            text = txt
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:limit]
    except:
        return ""

async def fetch_urls(urls, limit_chars=12000):
    out=[]
    for u in urls[:3]:
        t = await fetch_url(u, limit=4000)
        if t: out.append(t)
    return "\n\n".join(out)[:limit_chars]

async def search_and_fetch(query:str, hits:int=2, limit_chars:int=12000):
    links=[]
    try:
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=hits, safesearch="moderate"):
                if r and r.get("href"): links.append(r["href"])
    except:
        pass
    return await fetch_urls(links, limit_chars) if links else ""

def read_txt(p): 
    return open(p,"r",encoding="utf-8",errors="ignore").read()

def read_pdf(p):
    return pdf_text(p) or ""

def read_docx(p):
    d=Docx(p)
    return "\n".join([x.text for x in d.paragraphs])

def read_table(p):
    if p.lower().endswith((".xlsx",".xls")): df=pd.read_excel(p)
    else: df=pd.read_csv(p)
    b=io.StringIO(); df.head(120).to_string(b); return b.getvalue()

def read_any(p):
    pl=p.lower()
    if pl.endswith((".txt",".md",".log")): return read_txt(p)
    if pl.endswith(".pdf"): return read_pdf(p)
    if pl.endswith(".docx"): return read_docx(p)
    if pl.endswith((".csv",".xlsx",".xls")): return read_table(p)
    return read_txt(p)

async def get_weather(city:str):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://wttr.in/{city}?format=3", timeout=15) as r:
                return await r.text()
    except:
        return "⚠️ Не удалось получить погоду."

async def get_currency(base="usd"):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://api.exchangerate.host/latest?base={base}", timeout=15) as r:
                data = await r.json()
                eur = data["rates"].get("EUR"); rub = data["rates"].get("RUB")
                if eur and rub: return f"💵 1 {base.upper()} = {eur:.2f} EUR | {rub:.2f} RUB"
                return "⚠️ Данных недостаточно."
    except:
        return "⚠️ Ошибка получения курсов."

async def get_news():
    try:
        results = DDGS().text("новости дня", max_results=3)
        items = [f"🗞 {r['title']} — {r['href']}" for r in results]
        return "\n".join(items) if items else "⚠️ Новости не найдены."
    except:
        return "⚠️ Новости не найдены."

async def get_fact():
    try:
        facts = DDGS().text("интересный факт", max_results=1)
        return facts[0]["body"] if facts else "⚠️ Факт не найден."
    except:
        return "⚠️ Факт не найден."

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("☀️ Погода", callback_data="weather")],
        [InlineKeyboardButton("💸 Курс валют", callback_data="currency")],
        [InlineKeyboardButton("🌍 Новости", callback_data="news")],
        [InlineKeyboardButton("🧠 Факт", callback_data="fact")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")]
    ])

def settings_menu(u):
    v = "Вкл" if u.get("voice", True) else "Выкл"
    t = u.get("translate_to","en").upper()
    l = (u.get("lang") or "ru").upper()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔊 Озвучка: {v}", callback_data="toggle_voice")],
        [InlineKeyboardButton(f"🌐 Язык интерфейса: {l}", callback_data="cycle_lang")],
        [InlineKeyboardButton(f"🎧 Язык перевода: {t}", callback_data="cycle_tr")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
    ])

async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u = await get_user(update.effective_user.id)
    await ctx.bot.send_message(chat_id=update.effective_chat.id, text="Привет, я Jarvis v2.5 Ultimate 🤖\nВыбери действие из меню:", reply_markup=main_menu())

async def cmd_reset(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u = await get_user(update.effective_user.id)
    await save_user(u["user_id"], [], u["mode"], u["voice"], u["lang"], u["translate_to"])
    await update.message.reply_text("🔁 Память очищена.")

async def cmd_weather(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not ctx.args: 
        await update.message.reply_text("Пример: /weather Москва")
        return
    city = " ".join(ctx.args)
    await update.message.reply_text(await get_weather(city))

async def cmd_currency(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    base = ctx.args[0] if ctx.args else "usd"
    await update.message.reply_text(await get_currency(base))

async def cmd_read(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts)<2:
        await update.message.reply_text("Формат: /read URL")
        return
    raw = await fetch_url(parts[1])
    if not raw:
        await update.message.reply_text("Не удалось прочитать страницу.")
        return
    sys=[{"role":"system","content":"Суммаризируй текст кратко и структурировано."}]
    out = await chat_completion(sys+[{"role":"user","content":raw[:16000]}], temperature=0.3)
    await update.message.reply_text(out[:4000])

async def on_document(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    f = await update.message.document.get_file()
    suffix = os.path.splitext(update.message.document.file_name or "")[1].lower() or ".bin"
    p = tempfile.mktemp(suffix=suffix)
    await f.download_to_drive(custom_path=p)
    loop = asyncio.get_event_loop()
    try:
        text = await loop.run_in_executor(None, read_any, p)
    except:
        text = ""
    if not text:
        await update.message.reply_text("Не удалось прочитать файл.")
        try: os.remove(p)
        except: pass
        return
    u = await get_user(update.effective_user.id)
    hist = u["memory"]
    msgs=[{"role":"system","content":"Ты Jarvis. Суммаризируй и извлекай ключевое."},{"role":"user","content":text[:16000]}]
    reply = await chat_completion(msgs, temperature=0.2, lang=u["lang"])
    hist.append({"role":"user","content":"[документ]"})
    hist.append({"role":"assistant","content":reply})
    await save_memory(u["user_id"], hist[-MEM_LIMIT:])
    await update.message.reply_text(reply[:4000])
    try: os.remove(p)
    except: pass

async def on_callback(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    u = await get_user(uid)
    data = q.data
    if data=="weather":
        await q.answer()
        await q.message.reply_text("Введи город: /weather Москва")
        return
    if data=="currency":
        await q.answer()
        await q.message.reply_text("Введи валюту: /currency usd")
        return
    if data=="news":
        await q.answer()
        await q.message.reply_text(await get_news())
        return
    if data=="fact":
        await q.answer()
        await q.message.reply_text(await get_fact())
        return
    if data=="settings":
        await q.answer()
        await q.message.reply_text("Настройки:", reply_markup=settings_menu(u))
        return
    if data=="toggle_voice":
        await q.answer("Переключено")
        v = not u["voice"]
        await save_user(uid, u["memory"], u["mode"], v, u["lang"], u["translate_to"])
        await q.message.edit_reply_markup(reply_markup=settings_menu({**u,"voice":v}))
        return
    if data=="cycle_lang":
        nxt = "en" if (u["lang"] or "ru")=="ru" else "ru"
        await save_user(uid, u["memory"], u["mode"], u["voice"], nxt, u["translate_to"])
        await q.answer("Готово")
        await q.message.edit_reply_markup(reply_markup=settings_menu({**u,"lang":nxt}))
        return
    if data=="cycle_tr":
        order=["en","ru","de","es","fr","it","tr","ar"]
        cur = u["translate_to"] or "en"
        try:
            i = (order.index(cur)+1)%len(order)
            tr = order[i]
        except:
            tr = "en"
        await save_user(uid, u["memory"], u["mode"], u["voice"], u["lang"], tr)
        await q.answer("Готово")
        await q.message.edit_reply_markup(reply_markup=settings_menu({**u,"translate_to":tr}))
        return
    if data=="back_home":
        await q.answer()
        await q.message.reply_text("Главное меню:", reply_markup=main_menu())
        return
    await q.answer("Ок")

async def on_text(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    t = (update.message.text or "").strip()
    if not t: return
    u = await get_user(uid)
    urls = extract_urls(t)
    web_snip = ""
    if urls:
        web_snip = await fetch_urls(urls)
    elif need_web(t):
        web_snip = await search_and_fetch(t, hits=2)
    hist = u["memory"]
    sys = [{"role":"system","content":f"Ты Jarvis — ассистент на {u['lang']}. Отвечай кратко и по делу."}]
    if web_snip:
        sys.append({"role":"system","content":"Актуальная сводка из интернета:\n"+web_snip})
    msgs = sys + hist + [{"role":"user","content":t}]
    reply = await chat_completion(msgs, temperature=0.5, lang=u["lang"])
    hist.append({"role":"user","content":t})
    hist.append({"role":"assistant","content":reply})
    await save_memory(uid, hist[-MEM_LIMIT:])
    await update.message.reply_text(reply[:4000])

async def on_voice(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    file = await (update.message.voice or update.message.audio).get_file()
    p = tempfile.mktemp(suffix=".ogg")
    await file.download_to_drive(custom_path=p)
    def stt_sync(fp:str):
        with open(fp,"rb") as f:
            r = oc_sync.audio.transcriptions.create(model="whisper-1", file=f)
        return (r.text or "").strip()
    text = await asyncio.to_thread(stt_sync, p)
    try: os.remove(p)
    except: pass
    if not text:
        await update.message.reply_text("Не удалось распознать голос.")
        return
    u = await get_user(uid)
    if text.lower().startswith("переведи") or text.lower().startswith("translate"):
        trg = u["translate_to"] or "en"
        tr = await chat_completion([
            {"role":"system","content":f"Переведи на {trg} и ничего больше не добавляй."},
            {"role":"user","content":text}
        ], temperature=0.2, lang=u["lang"])
        mp3_path = await asyncio.to_thread(tts_to_file_sync, tr)
        try:
            with open(mp3_path,"rb") as f:
                await update.message.reply_voice(voice=InputFile(f, filename="jarvis.mp3"))
        finally:
            try: os.remove(mp3_path)
            except: pass
        return
    hist = u["memory"]
    sys = [{"role":"system","content":f"Ты Jarvis — ассистент на {u['lang']}. Отвечай по делу."}]
    msgs = sys + hist + [{"role":"user","content":text}]
    reply = await chat_completion(msgs, temperature=0.6, lang=u["lang"])
    hist.append({"role":"user","content":text})
    hist.append({"role":"assistant","content":reply})
    await save_memory(uid, hist[-MEM_LIMIT:])
    if u["voice"]:
        mp3_path = await asyncio.to_thread(tts_to_file_sync, reply)
        send_as_voice = True
        try:
            with open(mp3_path,"rb") as f:
                await update.message.reply_voice(voice=InputFile(f, filename="jarvis.mp3"))
        except:
            send_as_voice = False
        finally:
            try: os.remove(mp3_path)
            except: pass
        if not send_as_voice:
            await update.message.reply_text(reply[:4000])
    else:
        await update.message.reply_text(reply[:4000])

async def health(request): 
    return aiohttp.web.Response(text="ok")

async def main():
    await init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("currency", cmd_currency))
    app.add_handler(CommandHandler("read", cmd_read))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    if WEBHOOK_URL:
        await app.bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
        await app.run_webhook(listen="0.0.0.0", port=PORT, url_path="tgwebhook", webhook_url=WEBHOOK_URL)
    else:
        await app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())
