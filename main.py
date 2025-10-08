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
MEM_LIMIT=int(os.getenv("MEMORY_LIMIT","25"))
LANG=os.getenv("LANGUAGE","ru")
BASE_URL=os.getenv("PUBLIC_URL","").rstrip("/")
PORT=int(os.getenv("PORT","10000"))
MIGRATION_KEY=os.getenv("MIGRATION_KEY","jarvis-fix-123")

UA="Mozilla/5.0"
SYS=f"Ты Jarvis — ассистент. Отвечай чётко и полезно."

oc=OpenAI(api_key=OPENAI_KEY)
application: Application|None=None

async def db_conn(): return await asyncpg.connect(DB_URL)

async def init_db():
    c=await db_conn()
    await c.execute("""
    create table if not exists users(
      user_id bigint primary key,
      memory jsonb default '[]'::jsonb,
      mode text default 'assistant',
      voice boolean default true,
      lang text default 'ru',
      translate_to text default null
    )
    """)
    await c.close()

async def get_user(uid:int):
    c=await db_conn()
    r=await c.fetchrow("select * from users where user_id=$1", uid)
    await c.close()
    if not r:
        await save_user(uid, [], "assistant", True, LANG, None)
        return {"user_id":uid,"memory":[],"mode":"assistant","voice":True,"lang":LANG,"translate_to":None}
    d=dict(r)
    v=d.get("memory",[])
    if isinstance(v,str):
        try: v=json.loads(v) if v else []
        except: v=[]
    d["memory"]=v or []
    return d

async def save_user(uid:int, memory, mode, voice, lang, translate_to):
    c=await db_conn()
    await c.execute("""
    insert into users(user_id,memory,mode,voice,lang,translate_to)
    values($1,$2,$3,$4,$5,$6)
    on conflict(user_id) do update set
      memory=excluded.memory, mode=excluded.mode, voice=excluded.voice,
      lang=excluded.lang, translate_to=excluded.translate_to
    """, uid, memory, mode, voice, lang, translate_to)
    await c.close()

def ask_openai(messages, temperature=0.4, max_tokens=900):
    r=oc.chat.completions.create(model=MODEL, messages=messages, temperature=temperature, max_tokens=max_tokens)
    return r.choices[0].message.content.strip()

def safe_tts(text:str):
    text=re.sub(r"[*_~`>#+=|{}<>\\]","",text)
    fn=tempfile.mktemp(suffix=".mp3")
    try:
        with oc.audio.speech.with_streaming_response.create(model=VOICE_MODEL, voice=VOICE_NAME, input=text) as resp:
            resp.stream_to_file(fn)
    except:
        open(fn,"wb").close()
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
    if pl.endswith(".pdf"): return read_pdf(p)
    if pl.endswith(".docx"): return read_docx(p)
    if pl.endswith((".csv",".xlsx",".xls")): return read_table(p)
    return read_txt(p)

async def fetch_url(url:str, limit=20000):
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent":UA}, timeout=25) as cl:
        r=await cl.get(url)
    ct=(r.headers.get("content-type") or "").lower()
    if "text/html" in ct or "<html" in (r.text[:500].lower() if r.text else ""):
        html=Document(r.text).summary()
        soup=BeautifulSoup(html,"lxml")
        text=soup.get_text("\n", strip=True)
    else:
        text=r.text or ""
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
    if not links: return ""
    return await fetch_urls(links, limit_chars)

def detect_mood(text:str):
    try:
        m=ask_openai(
            [{"role":"system","content":"Определи настроение кратко: радость/спокойствие/усталость/грусть/злость/нейтрально."},
             {"role":"user","content":text}],
            temperature=0.2, max_tokens=12
        )
        return m.lower()
    except:
        return "нейтрально"

def empathy_reply(text,mood,mode):
    if mode!="friend": return None
    if any(k in mood for k in ["грусть","устал","усталость"]): return "Понимаю 😌. Хочешь, подскажу что поможет выдохнуть?"
    if any(k in mood for k in ["злость","раздраж"]): return "Похоже, было непросто 😅. Давай разберём, что можно улучшить?"
    if any(k in mood for k in ["рад","радость","счаст"]): return "Кайф! 😊 Рад за тебя!"
    return "Я рядом. Если нужно — помогу."

async def get_weather(city:str):
    try:
        async with httpx.AsyncClient() as cl:
            r=await cl.get(f"https://wttr.in/{city}?format=3")
        return "☀️ "+r.text.strip()
    except:
        return "Не удалось получить погоду."

async def get_currency(code:str):
    try:
        async with httpx.AsyncClient() as cl:
            r=await cl.get(f"https://api.exchangerate.host/latest?base={code.upper()}&symbols=USD,EUR,RUB")
        d=r.json()["rates"]
        return "💸 "+code.upper()+"\n"+"\n".join([f"{k}: {v:.2f}" for k,v in d.items()])
    except:
        return "Ошибка курса валют."

async def get_news():
    txt=await search_and_fetch("главные новости дня", hits=3)
    if not txt: return "Нет новостей."
    s=ask_openai([{"role":"system","content":"Сделай краткий структурированный обзор новостей в 5 пунктах."},{"role":"user","content":txt}])
    return "📰 "+s

def random_fact():
    facts=[
        "🧠 У улитки до 25 000 зубов.",
        "🌍 Ежедневно рождается ~385 000 человек.",
        "⚡ Молния горячее поверхности Солнца.",
        "💡 Первую веб-страницу создал Тим Бернерс-Ли в 1991.",
        "🎧 Музыка помогает восстанавливать память."
    ]
    return random.choice(facts)

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("☀️ Погода","weather"), InlineKeyboardButton("💸 Курс","currency")],
        [InlineKeyboardButton("🌍 Новости","news"), InlineKeyboardButton("🧠 Факт","fact")],
        [InlineKeyboardButton("🎧 Перевод","translate"), InlineKeyboardButton("⚙️ Настройки","settings")],
        [InlineKeyboardButton("📝 Контент","content")]
    ])

def settings_menu(u):
    v="🔊 Вкл" if u["voice"] else "🔇 Выкл"
    mode="🤖 Ассистент" if u["mode"]=="assistant" else "🧠 Друг"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Режим: {mode}","toggle_mode"), InlineKeyboardButton(f"Озвучка: {v}","toggle_voice")],
        [InlineKeyboardButton("⬅️ Назад","back")]
    ])

def content_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💡 Идеи /idea","c_idea"), InlineKeyboardButton("🗞️ Подпись /caption","c_caption")],
        [InlineKeyboardButton("🎬 Сценарий /script","c_script"), InlineKeyboardButton("🧾 Статья /article","c_article")],
        [InlineKeyboardButton("⬅️ Назад","back")]
    ])

async def set_menu(app:Application):
    await app.bot.set_my_commands([
        BotCommand("start","меню"),
        BotCommand("reset","сброс памяти"),
        BotCommand("translate","перевод голосовых"),
        BotCommand("weather","погода"),
        BotCommand("currency","курс валют"),
        BotCommand("news","новости дня"),
        BotCommand("fact","случайный факт"),
        BotCommand("idea","идеи контента"),
        BotCommand("caption","подпись к посту"),
        BotCommand("script","сценарий видео"),
        BotCommand("article","статья по тезисам"),
    ])

def content_prompt(kind:str, args:str, lang:str, mode:str):
    base_style="дружелюбно и по делу" if mode=="assistant" else "эмпатично, живо и мотивирующе"
    if kind=="idea":
        sys=f"Ты генератор идей. Дай 10 идей постов с короткими тезисами. Стиль: {base_style}. Язык: {lang}."
    elif kind=="caption":
        sys=f"Ты пишешь подписи. Дай 5 вариантов подписей к посту с призывом к действию и эмодзи. Язык: {lang}. Стиль: {base_style}."
    elif kind=="script":
        sys=f"Ты сценарист. Сделай структурированный сценарий короткого ролика: hook, value, CTA. Язык: {lang}. Стиль: {base_style}."
    else:
        sys=f"Ты пишешь статьи. Сформируй связный материал с заголовками и пунктами. Язык: {lang}. Стиль: {base_style}."
    return [{"role":"system","content":sys},{"role":"user","content":args.strip()}]

async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await set_menu(ctx.application)
    await update.message.reply_text("Привет, я Jarvis v2.2 Ultimate 🤖", reply_markup=main_menu())

async def cmd_reset(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    await save_user(u["user_id"], [], u["mode"], u["voice"], u["lang"], u["translate_to"])
    await update.message.reply_text("Память очищена 🧹")

async def cmd_translate(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    parts=(update.message.text or "").split()
    if len(parts)<2 or parts[1].lower()=="off":
        await save_user(u["user_id"], u["memory"], u["mode"], u["voice"], u["lang"], None)
        return await update.message.reply_text("Перевод голосовых выключен.")
    trg=parts[1].strip().lower()
    await save_user(u["user_id"], u["memory"], u["mode"], u["voice"], u["lang"], trg)
    await update.message.reply_text(f"Теперь перевожу голосовые на: {trg.upper()}")

async def cmd_weather(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    city=parts[1] if len(parts)>1 else "Москва"
    await update.message.reply_text(await get_weather(city))

async def cmd_currency(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    code=parts[1] if len(parts)>1 else "usd"
    await update.message.reply_text(await get_currency(code))

async def cmd_news(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text((await get_news())[:4000])

async def cmd_fact(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(random_fact())

async def cmd_idea(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    args=(update.message.text or "").split(maxsplit=1)
    topic=args[1] if len(args)>1 else "Instagram про бизнес"
    out=ask_openai(content_prompt("idea", topic, u["lang"], u["mode"]))
    await update.message.reply_text(out[:4000])

async def cmd_caption(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    args=(update.message.text or "").split(maxsplit=1)
    brief=args[1] if len(args)>1 else "Пост про запуск продукта"
    out=ask_openai(content_prompt("caption", brief, u["lang"], u["mode"]))
    await update.message.reply_text(out[:4000])

async def cmd_script(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    args=(update.message.text or "").split(maxsplit=1)
    brief=args[1] if len(args)>1 else "Рилс о пользе AI в бизнесе"
    out=ask_openai(content_prompt("script", brief, u["lang"], u["mode"]))
    await update.message.reply_text(out[:4000])

async def cmd_article(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    args=(update.message.text or "").split(maxsplit=1)
    brief=args[1] if len(args)>1 else "Как запустить телеграм-бота"
    out=ask_openai(content_prompt("article", brief, u["lang"], u["mode"]))
    await update.message.reply_text(out[:4000])

async def on_callback(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    u=await get_user(q.from_user.id)
    d=q.data
    if d=="weather": await q.edit_message_text("Введи: /weather Город")
    elif d=="currency": await q.edit_message_text("Введи: /currency usd")
    elif d=="news": await q.edit_message_text((await get_news())[:4000])
    elif d=="fact": await q.edit_message_text(random_fact())
    elif d=="translate": await q.edit_message_text("Отправь голосовое. Язык: /translate en")
    elif d=="settings": await q.edit_message_text("Настройки:", reply_markup=settings_menu(u))
    elif d=="toggle_voice":
        await save_user(u["user_id"],u["memory"],u["mode"],not u["voice"],u["lang"],u["translate_to"])
        uu=await get_user(u["user_id"])
        await q.edit_message_text("Ок.", reply_markup=settings_menu(uu))
    elif d=="toggle_mode":
        nm="friend" if u["mode"]=="assistant" else "assistant"
        await save_user(u["user_id"],u["memory"],nm,u["voice"],u["lang"],u["translate_to"])
        uu=await get_user(u["user_id"])
        await q.edit_message_text("Режим переключён.", reply_markup=settings_menu(uu))
    elif d=="content":
        await q.edit_message_text("Контент-центр:", reply_markup=content_menu())
    elif d=="c_idea":
        await q.edit_message_text("Команда: /idea тема")
    elif d=="c_caption":
        await q.edit_message_text("Команда: /caption краткий бриф")
    elif d=="c_script":
        await q.edit_message_text("Команда: /script тема")
    elif d=="c_article":
        await q.edit_message_text("Команда: /article тезисы")
    elif d=="back":
        await q.edit_message_text("Главное меню:", reply_markup=main_menu())

async def on_voice(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    v=update.message.voice or update.message.audio
    if not v: return
    f=await ctx.bot.get_file(v.file_id)
    p=await f.download_to_drive()
    text=await asyncio.get_event_loop().run_in_executor(None, transcribe, p)
    if not text: return await update.message.reply_text("Не удалось распознать.")
    if u["translate_to"]:
        translated=ask_openai([{"role":"system","content":f"Переведи на {u['translate_to']}. Без пояснений."},{"role":"user","content":text}])
        mp3=safe_tts(translated)
        try:
            await update.message.reply_voice(InputFile(mp3))
        finally:
            try: os.remove(mp3)
            except: pass
        return
    mood=detect_mood(text)
    hist=u["memory"]
    msgs=[{"role":"system","content":SYS}, *hist, {"role":"user","content":text}]
    reply=await asyncio.to_thread(ask_openai, msgs)
    em=empathy_reply(text, mood, u["mode"])
    if em: reply=f"{reply}\n\n{em}"
    hist.append({"role":"user","content":text}); hist.append({"role":"assistant","content":reply})
    await save_user(u["user_id"], hist[-int(MEM_LIMIT):], u["mode"], u["voice"], u["lang"], u["translate_to"])
    if u["voice"]:
        mp3=safe_tts(reply)
        try:
            await update.message.reply_voice(InputFile(mp3))
        finally:
            try: os.remove(mp3)
            except: pass
    else:
        await update.message.reply_text(reply[:4000])

async def on_document(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    doc=update.message.document
    if not doc: return
    f=await ctx.bot.get_file(doc.file_id)
    p=await f.download_to_drive()
    loop=asyncio.get_event_loop()
    txt=await loop.run_in_executor(None, read_any, p)
    if not txt: return await update.message.reply_text("Не удалось прочитать файл.")
    s=ask_openai([{"role":"system","content":"Суммаризируй текст кратко и структурировано."},{"role":"user","content":txt[:16000]}])
    await update.message.reply_text(s[:4000])

async def on_text(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    t=(update.message.text or update.message.caption or "").strip()
    if not t: return
    if t.startswith("/weather"): return await cmd_weather(update, ctx)
    if t.startswith("/currency"): return await cmd_currency(update, ctx)
    if t.startswith("/translate"): return await cmd_translate(update, ctx)
    if t.startswith("/news"): return await cmd_news(update, ctx)
    if t.startswith("/fact"): return await cmd_fact(update, ctx)
    if t.startswith("/idea"): return await cmd_idea(update, ctx)
    if t.startswith("/caption"): return await cmd_caption(update, ctx)
    if t.startswith("/script"): return await cmd_script(update, ctx)
    if t.startswith("/article"): return await cmd_article(update, ctx)
    urls=extract_urls(t)
    web_snip=""
    if urls:
        try: web_snip=await fetch_urls(urls)
        except: web_snip=""
    elif need_web(t):
        try: web_snip=await search_and_fetch(t, hits=2)
        except: web_snip=""
    mood=detect_mood(t)
    hist=u["memory"]
    msgs=[{"role":"system","content":SYS}]
    if web_snip: msgs.append({"role":"system","content":"Сводка из интернета:\n"+web_snip})
    msgs+=hist+[{"role":"user","content":t}]
    reply=await asyncio.to_thread(ask_openai, msgs)
    em=empathy_reply(t, mood, u["mode"])
    if em: reply=f"{reply}\n\n{em}"
    hist.append({"role":"user","content":t}); hist.append({"role":"assistant","content":reply})
    await save_user(uid, hist[-int(MEM_LIMIT):], u["mode"], u["voice"], u["lang"], u["translate_to"])
    await update.message.reply_text(reply[:4000])

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
    data=await request.json()
    upd=Update.de_json(data, application.bot)
    await application.process_update(upd)
    return web.Response(text="ok")

def build_app()->Application:
    app=ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("translate", cmd_translate))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("currency", cmd_currency))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("fact", cmd_fact))
    app.add_handler(CommandHandler("idea", cmd_idea))
    app.add_handler(CommandHandler("caption", cmd_caption))
    app.add_handler(CommandHandler("script", cmd_script))
    app.add_handler(CommandHandler("article", cmd_article))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
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
