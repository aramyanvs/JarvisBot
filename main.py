import os, re, json, io, tempfile, asyncio, random
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

OPENAI_KEY=os.getenv("OPENAI_API_KEY","")
DB_URL=os.getenv("DB_URL","")
BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
ADMIN_ID=int(os.getenv("ADMIN_ID","0"))
MODEL=os.getenv("OPENAI_MODEL","gpt-4o")
VOICE_MODEL=os.getenv("VOICE_MODEL","gpt-4o-mini-tts")
VOICE_NAME=os.getenv("VOICE_NAME","alloy")
MEM_LIMIT=int(os.getenv("MEMORY_LIMIT","1500"))
LANG=os.getenv("LANGUAGE","ru")
BASE_URL=os.getenv("PUBLIC_URL","").rstrip("/")
PORT=int(os.getenv("PORT","10000"))
VOICE_MODE=os.getenv("VOICE_MODE","true").lower()=="true"
MIGRATION_KEY=os.getenv("MIGRATION_KEY","jarvis-fix-123")

UA="Mozilla/5.0"
SYS=f"Ты Jarvis — ассистент на {LANG}. Отвечай по делу, дружелюбно и кратко, но по сути."

oc=OpenAI(api_key=OPENAI_KEY)
application: Application|None=None

async def db_conn(): return await asyncpg.connect(DB_URL)

async def init_db():
    c=await db_conn()
    await c.execute("""
    create table if not exists users(
      user_id bigint primary key,
      memory jsonb default '[]'::jsonb,
      lang text default 'ru',
      voice boolean default true,
      translate_to text default null,
      personality text default 'assistant',
      style text default 'short'
    )""")
    await c.close()

async def get_user(uid:int):
    c=await db_conn()
    r=await c.fetchrow("select user_id,memory,lang,voice,translate_to,personality,style from users where user_id=$1", uid)
    await c.close()
    if not r:
        await save_user(uid, [], LANG or "ru", True, None, "assistant", "short")
        return {"user_id":uid,"memory":[],"lang":LANG or "ru","voice":True,"translate_to":None,"personality":"assistant","style":"short"}
    d=dict(r)
    v=d.get("memory",[])
    if isinstance(v,str):
        try: v=json.loads(v) if v else []
        except: v=[]
    d["memory"]=v or []
    return d

async def save_user(uid:int, memory=None, lang=None, voice=None, translate_to=None, personality=None, style=None):
    c=await db_conn()
    await c.execute("""
    insert into users(user_id,memory,lang,voice,translate_to,personality,style)
    values($1,$2,$3,$4,$5,$6,$7)
    on conflict(user_id) do update set
      memory=excluded.memory, lang=excluded.lang, voice=excluded.voice,
      translate_to=excluded.translate_to, personality=excluded.personality, style=excluded.style
    """, uid, memory if memory is not None else [], lang or (LANG or "ru"),
       True if voice is None else voice, translate_to, personality or "assistant", style or "short")
    await c.close()

def ask_openai(messages, temperature=0.4, max_tokens=900):
    r=oc.chat.completions.create(model=MODEL, messages=messages, temperature=temperature, max_tokens=max_tokens)
    return r.choices[0].message.content.strip()

def tts_to_mp3(text:str):
    fn=tempfile.mktemp(suffix=".mp3")
    with oc.audio.speech.with_streaming_response.create(model=VOICE_MODEL, voice=VOICE_NAME, input=text) as resp:
        resp.stream_to_file(fn)
    return fn

def transcribe(path:str):
    with open(path,"rb") as f:
        r=oc.audio.transcriptions.create(model="whisper-1", file=f)
    return (r.text or "").strip()

def read_txt(p): return open(p,"r",encoding="utf-8",errors="ignore").read()
def read_pdf(p): return pdf_text(p) or ""
def read_docx(p): d=Docx(p); return "\n".join([x.text for x in d.paragraphs])
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

async def fetch_url(url:str, limit=20000):
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent":UA}, timeout=25) as cl:
        r=await cl.get(url)
    ct=(r.headers.get("content-type") or "").lower()
    if "text/html" in ct or "<html" in r.text[:500].lower():
        html=Document(r.text).summary()
        soup=BeautifulSoup(html,"lxml")
        text=soup.get_text("\n", strip=True)
    else:
        text=r.text
    return re.sub(r"\n{3,}", "\n\n", text)[:limit]

def extract_urls(q:str): return re.findall(r"https?://\S+", q)

async def fetch_urls(urls, limit_chars=15000):
    out=[]
    for u in urls[:3]:
        try:
            t=await fetch_url(u, limit=4000)
            if t: out.append(t)
        except: pass
    return "\n\n".join(out)[:limit_chars]

def need_web(q:str):
    t=q.lower()
    keys=["сейчас","сегодня","новост","курс","цена","сколько стоит","когда будет","последн","обнов","релиз","погода","расписан","матч","акции","доступно","вышел","итог"]
    if any(k in t for k in keys): return True
    if re.search(r"\b20(2[4-9]|3\d)\b", t): return True
    if "http://" in t or "https://" in t: return True
    return False

async def search_and_fetch(query:str, hits:int=2, limit_chars:int=15000):
    links=[]
    try:
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=hits, safesearch="moderate"):
                if r and r.get("href"): links.append(r["href"])
    except: pass
    return await fetch_urls(links, limit_chars) if links else ""

async def weather(city:str):
    try:
        async with httpx.AsyncClient() as cl:
            r=await cl.get(f"https://wttr.in/{city}?format=3")
            return r.text.strip()
    except: return "Не удалось получить погоду"

async def currency_rate(code:str):
    try:
        async with httpx.AsyncClient() as cl:
            r=await cl.get(f"https://api.exchangerate.host/latest?base={code.upper()}&symbols=USD,EUR,RUB")
            d=r.json()["rates"]
            return "💸 "+code.upper()+":\n"+"\n".join([f"{k}: {v:.2f}" for k,v in d.items()])
    except: return "Не удалось получить курс"

async def news_digest():
    txt=await search_and_fetch("главные новости дня", hits=3)
    if not txt: return "Нет данных"
    s=ask_openai([{"role":"system","content":"Сделай краткий обзор новостей."},{"role":"user","content":txt}])
    return s

def style_refine(text:str, personality:str, style:str):
    p="Ассистент"
    if personality=="professor": p="Профессор"
    if personality=="sarcastic": p="Саркастичный помощник"
    s="кратко" if style=="short" else "подробно"
    try:
        return ask_openai([{"role":"system","content":f"Говори как {p}. Отвечай {s}."},{"role":"user","content":text}])
    except:
        return text

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("☀️ Погода","weather"), InlineKeyboardButton("💸 Курс","currency")],
        [InlineKeyboardButton("🌍 Новости","news"), InlineKeyboardButton("🧠 Факт","fact")],
        [InlineKeyboardButton("🎧 Перевод","translate"), InlineKeyboardButton("⚙️ Настройки","settings")]
    ])

def settings_menu(voice_on:bool, lang:str, personality:str, style:str):
    v="🔊 Вкл" if voice_on else "🔇 Выкл"
    p={"assistant":"Ассистент","professor":"Профессор","sarcastic":"Сарказм"}.get(personality,"Ассистент")
    s={"short":"Кратко","long":"Подробно"}.get(style,"Кратко")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Язык: {lang}","set_lang"), InlineKeyboardButton(f"Озвучка: {v}","toggle_voice")],
        [InlineKeyboardButton(f"Персона: {p}","personality"), InlineKeyboardButton(f"Стиль: {s}","style")],
        [InlineKeyboardButton("⬅️ Назад","back")]
    ])

def personalities_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Ассистент","p_assistant"), InlineKeyboardButton("🧙 Профессор","p_professor"), InlineKeyboardButton("😏 Сарказм","p_sarcastic")],
        [InlineKeyboardButton("⬅️ Назад","settings")]
    ])

def styles_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Кратко","s_short"), InlineKeyboardButton("Подробно","s_long")],
        [InlineKeyboardButton("⬅️ Назад","settings")]
    ])

async def set_menu(app:Application):
    await app.bot.set_my_commands([
        BotCommand("start","меню"),
        BotCommand("ping","проверка"),
        BotCommand("reset","сброс памяти"),
        BotCommand("weather","погода"),
        BotCommand("currency","курс валют"),
        BotCommand("translate","переводчик"),
        BotCommand("personality","персона"),
    ])

async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await set_menu(ctx.application)
    await update.message.reply_text("Привет, я Jarvis v2 Ultimate 🤖", reply_markup=main_menu())

async def cmd_ping(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def cmd_reset(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    await save_user(u["user_id"], [], u["lang"], u["voice"], u["translate_to"], u["personality"], u["style"])
    await update.message.reply_text("Память очищена 🧹")

async def cmd_translate(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    parts=(update.message.text or "").split()
    if len(parts)<2 or parts[1].lower()=="off":
        await save_user(u["user_id"], u["memory"], u["lang"], u["voice"], None, u["personality"], u["style"])
        return await update.message.reply_text("Перевод выключен")
    trg=parts[1].strip().lower()
    await save_user(u["user_id"], u["memory"], u["lang"], u["voice"], trg, u["personality"], u["style"])
    await update.message.reply_text(f"Теперь перевожу голосовые на: {trg}")

async def cmd_personality(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выбери персону:", reply_markup=personalities_menu())

async def on_callback(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    u=await get_user(q.from_user.id)
    d=q.data
    if d=="weather": await q.edit_message_text("Введи город для погоды")
    elif d=="currency": await q.edit_message_text("Введи код валюты, например usd")
    elif d=="news":
        s=await news_digest()
        await q.edit_message_text(s[:4000])
    elif d=="fact":
        f=ask_openai([{"role":"system","content":"Расскажи интересный факт в 1-2 предложениях."},{"role":"user","content":"Факт"}])
        await q.edit_message_text("🧠 "+f)
    elif d=="translate":
        await q.edit_message_text("Отправь голосовое. Для выбора языка: /translate en")
    elif d=="settings":
        await q.edit_message_text("Настройки:", reply_markup=settings_menu(u["voice"], u["lang"], u["personality"], u["style"]))
    elif d=="set_lang":
        await q.edit_message_text("Введи язык интерфейса (ru|en)")
    elif d=="toggle_voice":
        await save_user(u["user_id"], u["memory"], u["lang"], not u["voice"], u["translate_to"], u["personality"], u["style"])
        u=await get_user(u["user_id"])
        await q.edit_message_text("Настройки:", reply_markup=settings_menu(u["voice"], u["lang"], u["personality"], u["style"]))
    elif d=="personality":
        await q.edit_message_text("Персона:", reply_markup=personalities_menu())
    elif d=="style":
        await q.edit_message_text("Стиль:", reply_markup=styles_menu())
    elif d=="p_assistant":
        await save_user(u["user_id"], u["memory"], u["lang"], u["voice"], u["translate_to"], "assistant", u["style"])
        await q.edit_message_text("Персона: Ассистент", reply_markup=settings_menu(u["voice"], u["lang"], "assistant", u["style"]))
    elif d=="p_professor":
        await save_user(u["user_id"], u["memory"], u["lang"], u["voice"], u["translate_to"], "professor", u["style"])
        await q.edit_message_text("Персона: Профессор", reply_markup=settings_menu(u["voice"], u["lang"], "professor", u["style"]))
    elif d=="p_sarcastic":
        await save_user(u["user_id"], u["memory"], u["lang"], u["voice"], u["translate_to"], "sarcastic", u["style"])
        await q.edit_message_text("Персона: Сарказм", reply_markup=settings_menu(u["voice"], u["lang"], "sarcastic", u["style"]))
    elif d=="s_short":
        await save_user(u["user_id"], u["memory"], u["lang"], u["voice"], u["translate_to"], u["personality"], "short")
        await q.edit_message_text("Стиль: кратко", reply_markup=settings_menu(u["voice"], u["lang"], u["personality"], "short"))
    elif d=="s_long":
        await save_user(u["user_id"], u["memory"], u["lang"], u["voice"], u["translate_to"], u["personality"], "long")
        await q.edit_message_text("Стиль: подробно", reply_markup=settings_menu(u["voice"], u["lang"], u["personality"], "long"))
    elif d=="back":
        await q.edit_message_text("Меню:", reply_markup=main_menu())

async def on_voice(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    f=await ctx.bot.get_file(update.message.voice.file_id)
    p=await f.download_to_drive()
    txt=await asyncio.get_event_loop().run_in_executor(None, transcribe, p)
    if u["translate_to"]:
        tr=ask_openai([{"role":"system","content":f"Переведи на {u['translate_to']}. Без пояснений."},{"role":"user","content":txt}])
        mp3=tts_to_mp3(tr); await update.message.reply_voice(InputFile(mp3)); 
        try: os.remove(mp3)
        except: pass
        await update.message.reply_text(tr[:4000])
        return
    hist=u["memory"]
    msgs=[{"role":"system","content":SYS}, *hist, {"role":"user","content":txt}]
    reply=await asyncio.to_thread(ask_openai, msgs)
    if u["personality"]!="assistant" or u["style"]!="short":
        try: reply=style_refine(reply, u["personality"], u["style"])
        except: pass
    hist.append({"role":"user","content":txt}); hist.append({"role":"assistant","content":reply})
    await save_user(u["user_id"], hist[-MEM_LIMIT:], u["lang"], u["voice"], u["translate_to"], u["personality"], u["style"])
    if VOICE_MODE and u["voice"]:
        mp3=tts_to_mp3(reply); await update.message.reply_voice(InputFile(mp3)); 
        try: os.remove(mp3)
        except: pass
    else:
        await update.message.reply_text(reply)

async def on_text(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    t=(update.message.text or "").strip()
    if t.startswith("/weather"):
        city=t.split(maxsplit=1)[1] if len(t.split())>1 else "Москва"
        return await update.message.reply_text(await weather(city))
    if t.startswith("/currency"):
        code=t.split(maxsplit=1)[1] if len(t.split())>1 else "usd"
        return await update.message.reply_text(await currency_rate(code))
    if t.startswith("/translate"):
        return await cmd_translate(update, ctx)
    if t.startswith("/personality"):
        return await cmd_personality(update, ctx)
    if t.lower() in ("ru","en"):
        await save_user(u["user_id"], u["memory"], t.lower(), u["voice"], u["translate_to"], u["personality"], u["style"])
        return await update.message.reply_text(f"Язык интерфейса: {t.lower()}")
    urls=extract_urls(t)
    web_snip=""
    if urls:
        try: web_snip=await fetch_urls(urls)
        except: web_snip=""
    elif need_web(t):
        try: web_snip=await search_and_fetch(t, hits=2)
        except: web_snip=""
    hist=u["memory"]
    msgs=[{"role":"system","content":SYS}]
    if web_snip: msgs.append({"role":"system","content":"Сводка из интернета:\n"+web_snip})
    msgs+=hist+[{"role":"user","content":t}]
    reply=await asyncio.to_thread(ask_openai, msgs)
    if u["personality"]!="assistant" or u["style"]!="short":
        try: reply=style_refine(reply, u["personality"], u["style"])
        except: pass
    hist.append({"role":"user","content":t}); hist.append({"role":"assistant","content":reply})
    await save_user(uid, hist[-MEM_LIMIT:], u["lang"], u["voice"], u["translate_to"], u["personality"], u["style"])
    await update.message.reply_text(reply)

async def health(request): return web.Response(text="ok")

async def migrate(request):
    if request.rel_url.query.get("key") != MIGRATION_KEY:
        return web.Response(status=403, text="forbidden")
    c=await asyncpg.connect(DB_URL)
    try:
        await c.execute("begin")
        await c.execute("update users set memory='[]' where memory is null or memory::text='' or not (memory is json)")
        await c.execute("alter table users alter column memory type jsonb using coalesce(nullif(trim(memory::text),''),'[]')::jsonb, alter column memory set default '[]'::jsonb")
        await c.execute("commit")
    except Exception as e:
        await c.execute("rollback")
        await c.close()
        return web.Response(text=str(e))
    await c.close()
    return web.Response(text="ok")

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
    app.add_handler(CommandHandler("translate", cmd_translate))
    app.add_handler(CommandHandler("personality", cmd_personality))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    return app

async def main():
    global application
    await init_db()
    application=build_app()
    await application.initialize()
    await application.start()
    aio=web.Application()
    aio.add_routes([web.get("/health", health)])
    aio.add_routes([web.get("/migrate", migrate)])
    aio.add_routes([web.post("/tgwebhook", tg_webhook)])
    runner=web.AppRunner(aio); await runner.setup()
    site=web.TCPSite(runner, "0.0.0.0", PORT); await site.start()
    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)
    await set_menu(application)
    print("READY"); print("WEBHOOK:", f"{BASE_URL}/tgwebhook", flush=True)
    await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(main())
