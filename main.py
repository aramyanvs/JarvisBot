import os, re, io, json, asyncio, tempfile
from dotenv import load_dotenv
load_dotenv()

import asyncpg, httpx, pandas as pd
from bs4 import BeautifulSoup
from readability import Document
from duckduckgo_search import DDGS
from pdfminer.high_level import extract_text as pdf_text
from docx import Document as Docx
from aiohttp import web
from openai import OpenAI

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, BotCommand
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

OPENAI_KEY=os.getenv("OPENAI_API_KEY","")
DB_URL=os.getenv("DB_URL","")
BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
ADMIN_ID=int(os.getenv("ADMIN_ID","0"))
MODEL=os.getenv("OPENAI_MODEL","gpt-4o")
MEM_LIMIT=int(os.getenv("MEMORY_LIMIT","1500"))
BASE_URL=os.getenv("PUBLIC_URL","").rstrip("/")
PORT=int(os.getenv("PORT","10000"))
VOICE_MODE=os.getenv("VOICE_MODE","true").lower()=="true"
DEFAULT_LANG=os.getenv("LANGUAGE","ru")
MIGRATION_KEY=os.getenv("MIGRATION_KEY","")

UA="Mozilla/5.0"
oc=OpenAI(api_key=OPENAI_KEY)
application: Application|None=None

async def db_conn():
    return await asyncpg.connect(DB_URL)

async def init_db():
    c=await db_conn()
    await c.execute("""
    create table if not exists users(
      user_id bigint primary key,
      memory jsonb default '[]'::jsonb,
      mode text default 'concise',
      voice boolean default true,
      lang text default 'ru',
      translate_to text default ''
    )""")
    await c.close()

async def get_user(uid:int):
    c=await db_conn()
    r=await c.fetchrow("select user_id,memory,mode,voice,lang,translate_to from users where user_id=$1", uid)
    if not r:
        await c.execute("insert into users(user_id,lang) values($1,$2) on conflict do nothing", uid, DEFAULT_LANG)
        r=await c.fetchrow("select user_id,memory,mode,voice,lang,translate_to from users where user_id=$1", uid)
    await c.close()
    mem=r["memory"]
    if isinstance(mem,str):
        try: mem=json.loads(mem) if mem else []
        except: mem=[]
    return {"user_id":r["user_id"],"memory":mem or [],"mode":r["mode"] or "concise","voice":bool(r["voice"]), "lang":r["lang"] or DEFAULT_LANG, "translate_to":r["translate_to"] or ""}

async def save_memory(uid:int, mem:list):
    c=await db_conn()
    await c.execute("update users set memory=$2 where user_id=$1", uid, json.dumps(mem))
    await c.close()

async def save_user(uid:int, mem:list, mode:str, voice:bool, lang:str, tr_to:str):
    c=await db_conn()
    await c.execute(
        "insert into users(user_id,memory,mode,voice,lang,translate_to) values($1,$2,$3,$4,$5,$6) on conflict(user_id) do update set memory=excluded.memory,mode=excluded.mode,voice=excluded.voice,lang=excluded.lang,translate_to=excluded.translate_to",
        uid, json.dumps(mem), mode, voice, lang, tr_to
    )
    await c.close()

def sys_preamble(lang:str, mode:str):
    s="Отвечай кратко и по делу." if mode=="concise" else "Отвечай развёрнуто и структурировано."
    return f"Ты Jarvis. Общайся на языке пользователя ({lang}). {s}"

def ask_openai(messages, temperature=0.3, max_tokens=800):
    r=oc.chat.completions.create(model=MODEL, messages=messages, temperature=temperature, max_tokens=max_tokens)
    return r.choices[0].message.content.strip()

async def http_get(url, timeout=25):
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent":UA}, timeout=timeout) as cl:
        return await cl.get(url)

async def fetch_url_text(url:str, limit=20000):
    r=await http_get(url)
    ct=(r.headers.get("content-type") or "").lower()
    if "text/html" in ct or "<html" in r.text[:1000].lower():
        html=Document(r.text).summary()
        soup=BeautifulSoup(html,"lxml")
        text=soup.get_text("\n", strip=True)
    elif "pdf" in ct or url.lower().endswith(".pdf"):
        fd=tempfile.mktemp(suffix=".pdf")
        with open(fd,"wb") as f: f.write(r.content)
        text=pdf_text(fd) or ""
        try: os.remove(fd)
        except: pass
    else:
        text=r.text
    text=re.sub(r"\n{3,}", "\n\n", text)
    return text[:limit]

def extract_urls(q:str):
    return re.findall(r"https?://\S+", q)

async def fetch_urls_combined(urls, limit_chars=12000):
    out=[]
    for u in urls[:3]:
        try:
            t=await fetch_url_text(u, limit=4000)
            if t: out.append(t)
        except: pass
    return "\n\n".join(out)[:limit_chars]

async def ddg_search_snippets(query:str, hits:int=3, limit_chars:int=12000):
    links=[]
    try:
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=hits, safesearch="moderate"):
                if r and r.get("href"): links.append(r["href"])
    except: pass
    return await fetch_urls_combined(links, limit_chars) if links else ""

def read_txt(p):
    return open(p,"r",encoding="utf-8",errors="ignore").read()
def read_pdf(p):
    return pdf_text(p) or ""
def read_docx(p):
    d=Docx(p); return "\n".join([x.text for x in d.paragraphs])
def read_table(p):
    if p.lower().endswith((".xlsx",".xls")): df=pd.read_excel(p)
    else: df=pd.read_csv(p)
    b=io.StringIO(); df.head(80).to_string(b); return b.getvalue()
def read_any(p):
    pl=p.lower()
    if pl.endswith((".txt",".md",".log")): return read_txt(p)
    if pl.endswith(".pdf"): return read_pdf(p)
    if pl.endswith(".docx"): return read_docx(p)
    if pl.endswith((".csv",".xlsx",".xls")): return read_table(p)
    return read_txt(p)

def transcribe(path:str):
    with open(path,"rb") as f:
        r=oc.audio.transcriptions.create(model="whisper-1", file=f)
    return (r.text or "").strip()

def tts_to_mp3(text:str, voice="alloy"):
    fn=tempfile.mktemp(suffix=".mp3")
    with oc.audio.speech.with_streaming_response.create(model="gpt-4o-mini-tts", voice=voice, input=text) as resp:
        resp.stream_to_file(fn)
    return fn

def detect_lang(text:str):
    sys=[{"role":"system","content":"Detect user language ISO-639-1 code only, no text."}]
    out=ask_openai(sys+[{"role":"user","content":text[:500]}], temperature=0)
    m=re.search(r"[a-z]{2}", out.lower())
    return m.group(0) if m else DEFAULT_LANG

async def set_menu(app:Application):
    await app.bot.set_my_commands([
        BotCommand("start","запуск"),
        BotCommand("ping","проверка"),
        BotCommand("read","прочитать URL"),
        BotCommand("reset","сброс памяти"),
        BotCommand("settings","настройки"),
        BotCommand("news","новости"),
        BotCommand("weather","погода"),
        BotCommand("currency","курс"),
        BotCommand("fact","факт"),
        BotCommand("translate_to","язык перевода голосовых"),
    ])

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("☀️ Погода", callback_data="menu_weather"),
         InlineKeyboardButton("💸 Курс", callback_data="menu_currency")],
        [InlineKeyboardButton("🌍 Новости", callback_data="menu_news"),
         InlineKeyboardButton("🧠 Факт", callback_data="menu_fact")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")]
    ])

def settings_menu(u):
    v="🔊 Озвучка: Вкл" if u["voice"] else "🔇 Озвучка: Выкл"
    m="📝 Стиль: Короткий" if u["mode"]=="concise" else "📝 Стиль: Подробный"
    t=f"🌐 Перевод голоса: {u['translate_to'] or 'выкл'}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(v, callback_data="set_voice")],
        [InlineKeyboardButton(m, callback_data="set_mode")],
        [InlineKeyboardButton(t, callback_data="set_trlang")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")]
    ])

async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await get_user(update.effective_user.id)
    await update.message.reply_text("Привет, я Jarvis v2 Ultimate 🤖", reply_markup=main_menu())

async def cmd_ping(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_reset(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    await save_user(u["user_id"], [], u["mode"], u["voice"], u["lang"], u["translate_to"])
    await update.message.reply_text("Память очищена.")

async def cmd_read(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    if len(parts)<2:
        await update.message.reply_text("Формат: /read URL")
        return
    try:
        raw=await fetch_url_text(parts[1])
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return
    summ=ask_openai([{"role":"system","content":"Суммаризируй кратко и структурировано."},{"role":"user","content":raw[:16000]}]) if len(raw)>1800 else raw
    await update.message.reply_text(summ[:4000])

async def cmd_settings(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    await update.message.reply_text("Настройки:", reply_markup=settings_menu(u))

async def on_button(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    uid=q.from_user.id
    u=await get_user(uid)
    await q.answer()
    if q.data=="menu_weather":
        await q.edit_message_text("Пришли город: /weather Москва")
    elif q.data=="menu_currency":
        await q.edit_message_text("Пришли код: /currency usd")
    elif q.data=="menu_news":
        await q.edit_message_text("Последние новости: /news")
    elif q.data=="menu_fact":
        await q.edit_message_text("Случайный факт: /fact")
    elif q.data=="menu_settings":
        await q.edit_message_reply_markup(reply_markup=settings_menu(u))
    elif q.data=="menu_back":
        await q.edit_message_reply_markup(reply_markup=main_menu())
    elif q.data=="set_voice":
        u["voice"]=not u["voice"]
        await save_user(uid,u["memory"],u["mode"],u["voice"],u["lang"],u["translate_to"])
        await q.edit_message_reply_markup(reply_markup=settings_menu(u))
    elif q.data=="set_mode":
        u["mode"]="detailed" if u["mode"]=="concise" else "concise"
        await save_user(uid,u["memory"],u["mode"],u["voice"],u["lang"],u["translate_to"])
        await q.edit_message_reply_markup(reply_markup=settings_menu(u))
    elif q.data=="set_trlang":
        await q.edit_message_text("Укажи язык перевода для голосовых, например: /translate_to en (пусто чтобы выключить)")

async def cmd_translate_to(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    parts=(update.message.text or "").split(maxsplit=1)
    trg=(parts[1].strip().lower() if len(parts)>1 else "")
    if trg and len(trg)>5: trg=trg[:5]
    await save_user(u["user_id"], u["memory"], u["mode"], u["voice"], u["lang"], trg)
    s=trg if trg else "выключен"
    await update.message.reply_text(f"Перевод голосовых: {s}")

async def cmd_weather(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    city=(parts[1] if len(parts)>1 else "Moscow")
    try:
        r=await http_get(f"https://wttr.in/{city}?format=3", timeout=15)
        await update.message.reply_text(r.text.strip()[:4000])
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def cmd_currency(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    code=(parts[1].strip().upper() if len(parts)>1 else "USD")
    try:
        r=await http_get(f"https://api.exchangerate.host/latest?base={code}", timeout=15)
        data=r.json()
        if "rates" in data:
            eur=data["rates"].get("EUR")
            rub=data["rates"].get("RUB")
            uah=data["rates"].get("UAH")
            msg=f"{code}-> EUR: {eur:.4f}, RUB: {rub:.2f}, UAH: {uah:.2f}"
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text("Нет данных.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def cmd_news(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    try:
        snip=await ddg_search_snippets("today world news", hits=4, limit_chars=8000)
        if not snip:
            await update.message.reply_text("Не нашёл новости.")
            return
        out=ask_openai([{"role":"system","content":"Сделай краткую сводку пунктами."},{"role":"user","content":snip[:15000]}])
        await update.message.reply_text(out[:4000])
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def cmd_fact(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    out=ask_openai([{"role":"system","content":"Дай один любопытный факт в 1-2 предложениях."},{"role":"user","content":"Факт"}], temperature=0.8)
    await update.message.reply_text(out[:1000])

def maybe_need_web(q:str):
    t=q.lower()
    keys=["сейчас","сегодня","новост","курс","цена","когда","последн","обнов","релиз","погода","расписан","акции","доступно","вышел","итог","fact-check","source","источник"]
    if any(k in t for k in keys): return True
    if re.search(r"\b20(2[4-9]|3\d)\b", t): return True
    if "http://" in t or "https://" in t: return True
    return False

def empathy_prefix(txt:str):
    mark=txt.lower()
    if any(w in mark for w in ["устал","вымотал","плохо","груст","печал","нерв","пережив","боюсь","волнуюсь"]):
        return "Понимаю твоё состояние. "
    if any(w in mark for w in ["ура","круто","рад","супер","отлично"]):
        return "Звучит здорово. "
    return ""

async def on_voice(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not VOICE_MODE:
        return
    v=update.message.voice or update.message.audio
    if not v:
        return
    f=await ctx.bot.get_file(v.file_id)
    p=await f.download_to_drive()
    loop=asyncio.get_event_loop()
    text=await loop.run_in_executor(None, transcribe, p)
    if not text:
        await update.message.reply_text("Не удалось распознать.")
        return
    uid=update.effective_user.id
    u=await get_user(uid)
    msgs=[{"role":"system","content":sys_preamble(u["lang"],u["mode"])}, *u["memory"], {"role":"user","content":text}]
    try:
        reply=await asyncio.to_thread(ask_openai, msgs)
    except Exception as e:
        reply=f"Ошибка: {e}"
    if u["translate_to"]:
        try:
            tr=ask_openai([{"role":"system","content":f"Переведи на {u['translate_to']} без пояснений."},{"role":"user","content":reply}], temperature=0)
            reply=tr
        except:
            pass
    u["memory"].append({"role":"user","content":text})
    u["memory"].append({"role":"assistant","content":reply})
    await save_memory(uid, u["memory"][-MEM_LIMIT:])
    if u["voice"]:
        mp3=tts_to_mp3(reply, voice="alloy")
        try:
            with open(mp3,"rb") as f:
                await update.message.reply_audio(InputFile(f, filename="jarvis.mp3"), caption=None)
        finally:
            try: os.remove(mp3)
            except: pass
    else:
        await update.message.reply_text(reply[:4000])

async def on_text(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    t=(update.message.text or update.message.caption or "").strip()
    if not t:
        return
    u=await get_user(uid)
    if t.startswith("/"):
        return
    urls=extract_urls(t)
    web_snip=""
    if urls:
        try: web_snip=await fetch_urls_combined(urls)
        except: web_snip=""
    elif maybe_need_web(t):
        try: web_snip=await ddg_search_snippets(t, hits=3)
        except: web_snip=""
    pre=empathy_prefix(t)
    msgs=[{"role":"system","content":sys_preamble(u["lang"],u["mode"])}]
    if web_snip: msgs.append({"role":"system","content":"Актуальная сводка:\n"+web_snip})
    msgs+=u["memory"]+[{"role":"user","content":t}]
    try:
        reply=await asyncio.to_thread(ask_openai, msgs)
    except Exception as e:
        reply=f"Ошибка: {e}"
    reply=pre+reply
    u["memory"].append({"role":"user","content":t})
    u["memory"].append({"role":"assistant","content":reply})
    await save_memory(uid, u["memory"][-MEM_LIMIT:])
    await update.message.reply_text(reply[:4000])

async def health(request):
    return web.Response(text="ok")

async def migrate(request):
    if request.rel_url.query.get("key") != MIGRATION_KEY or not MIGRATION_KEY:
        return web.Response(status=403, text="forbidden")
    c=await db_conn()
    try:
        await c.execute("begin")
        await c.execute("update users set memory='[]' where memory is null or memory::text='' or not (memory is json)")
        await c.execute("commit")
        await c.close()
        return web.Response(text="ok")
    except Exception as e:
        await c.execute("rollback")
        await c.close()
        return web.Response(text=str(e))

async def tg_webhook(request):
    try:
        data=await request.json()
        upd=Update.de_json(data, application.bot)
        await application.process_update(upd)
        return web.Response(text="ok")
    except Exception as e:
        return web.Response(status=200, text=str(e))

def build_app()->Application:
    app=ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("read", cmd_read))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("translate_to", cmd_translate_to))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("currency", cmd_currency))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("fact", cmd_fact))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

async def main():
    global application
    await init_db()
    application=build_app()
    await application.initialize()
    await application.start()
    aio=web.Application()
    aio.add_routes([web.get("/health", health)])
    aio.add_routes([web.post("/tgwebhook", tg_webhook)])
    aio.add_routes([web.get("/migrate", migrate)])
    runner=web.AppRunner(aio); await runner.setup()
    site=web.TCPSite(runner, "0.0.0.0", PORT); await site.start()
    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)
    await set_menu(application)
    print("READY"); print("WEBHOOK:", f"{BASE_URL}/tgwebhook", flush=True)
    await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(main())
