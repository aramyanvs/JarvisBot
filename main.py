import os, io, re, json, asyncio, time, math, random, hashlib
import aiohttp
import asyncpg
import httpx
from aiohttp import web
from bs4 import BeautifulSoup
from readability import Document
from duckduckgo_search import DDGS
from pdfminer.high_level import extract_text as pdf_extract_text
from docx import Document as DocxDocument
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
OPENAI_KEY=os.getenv("OPENAI_API_KEY","")
OPENAI_MODEL=os.getenv("OPENAI_MODEL","gpt-4o-mini")
DB_URL=os.getenv("DB_URL","")
BASE_URL=os.getenv("BASE_URL","")
DEFAULT_LANG=os.getenv("LANGUAGE","ru")
MEM_LIMIT=int(os.getenv("MEMORY_LIMIT","1500"))
VOICE_ENABLED=(os.getenv("VOICE_MODE","true").lower()=="true")
ALWAYS_WEB=(os.getenv("ALWAYS_WEB","false").lower()=="true")
PORT=int(os.getenv("PORT","8080"))

application=None

async def db_conn(): 
    return await asyncpg.connect(DB_URL)

async def init_db():
    c=await db_conn()
    await c.execute("create table if not exists users (user_id bigint primary key, lang text default $1, tts_enabled boolean default $2, voice text default $3, personality text default $4, translate_to text default $5, web_mode text default $6)", DEFAULT_LANG, VOICE_ENABLED, "alloy", "assistant", "en", "auto")
    await c.execute("create table if not exists memory (user_id bigint references users(user_id) on delete cascade, role text, content text, ts timestamptz default now())")
    await c.close()

async def get_user(uid:int):
    c=await db_conn()
    row=await c.fetchrow("select user_id,lang,tts_enabled,voice,personality,translate_to,web_mode from users where user_id=$1", uid)
    if not row:
        await c.execute("insert into users(user_id) values($1)", uid)
        row=await c.fetchrow("select user_id,lang,tts_enabled,voice,personality,translate_to,web_mode from users where user_id=$1", uid)
    await c.close()
    return dict(row)

async def save_user(u):
    c=await db_conn()
    await c.execute("update users set lang=$2, tts_enabled=$3, voice=$4, personality=$5, translate_to=$6, web_mode=$7 where user_id=$1", u["user_id"], u["lang"], u["tts_enabled"], u["voice"], u["personality"], u["translate_to"], u["web_mode"])
    await c.close()

async def get_memory(uid:int):
    c=await db_conn()
    rows=await c.fetch("select role,content from memory where user_id=$1 order by ts asc", uid)
    await c.close()
    return [{"role":r["role"],"content":r["content"]} for r in rows]

async def add_memory(uid:int, role:str, content:str):
    c=await db_conn()
    await c.execute("insert into memory(user_id,role,content) values($1,$2,$3)", uid, role, content)
    await c.close()

async def trim_memory(uid:int):
    c=await db_conn()
    rows=await c.fetch("select ctid,content from memory where user_id=$1 order by ts asc", uid)
    total=sum(len(r["content"]) for r in rows)
    idx=0
    while total>MEM_LIMIT and idx<len(rows):
        await c.execute("delete from memory where ctid=$1", rows[idx]["ctid"])
        total-=len(rows[idx]["content"])
        idx+=1
    await c.close()

def main_menu():
    kb=[[InlineKeyboardButton("☀️ Погода","weather"),InlineKeyboardButton("💸 Курс","currency")],
        [InlineKeyboardButton("🌍 Новости","news"),InlineKeyboardButton("🧠 Факт","fact")],
        [InlineKeyboardButton("⚙️ Настройки","settings")]]
    return InlineKeyboardMarkup(kb)

def settings_menu(u):
    kb=[
        [InlineKeyboardButton(f"🌐 Язык: {u['lang']}", callback_data="set_lang")],
        [InlineKeyboardButton(f"🔊 Озвучка: {'вкл' if u['tts_enabled'] else 'выкл'}", callback_data="toggle_tts"), InlineKeyboardButton(f"🎙️ Голос: {u['voice']}", callback_data="set_voice")],
        [InlineKeyboardButton(f"💬 Стиль: {u['personality']}", callback_data="set_personality")],
        [InlineKeyboardButton(f"🌐 Веб: {u['web_mode']}", callback_data="set_webmode")],
        [InlineKeyboardButton(f"🌍 Перевод в: {u['translate_to']}", callback_data="set_translate_to")],
        [InlineKeyboardButton("⬅️ Назад","back_home")]
    ]
    return InlineKeyboardMarkup(kb)

async def http_get(url, timeout=15):
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as cl:
        r=await cl.get(url, headers={"User-Agent":"Mozilla/5.0"})
        r.raise_for_status()
        return r

async def fetch_weather(q):
    url=f"https://wttr.in/{q}?format=%l:+%c+%t,+ветер+%w,+ощущается+%f"
    r=await http_get(url,10)
    return r.text.strip()

async def fetch_currency(base):
    base=base.upper()
    r=await http_get(f"https://api.exchangerate.host/latest?base={base}",15)
    data=r.json()
    rates=data.get("rates",{})
    out=[f"{base} курс:"]
    for t in ["USD","EUR","RUB","AED","KZT","TRY"]:
        if t in rates:
            out.append(f"{t}: {rates[t]:.4f}")
    return "\n".join(out)

async def ddg_news(q="news", n=5):
    out=[]
    async with DDGS() as ddgs:
        async for r in ddgs.news(keywords=q, max_results=n, region="ru-ru"):
            out.append(f"• {r.get('title','')} — {r.get('date','')}\n{r.get('url','')}")
    return "\n".join(out) if out else "Новостей не найдено."

async def random_fact():
    txt=await ddg_news("interesting facts",3)
    return "Случайный факт:\n"+txt

def detect_lang(text):
    cyr=sum(1 for ch in text if "а"<=ch.lower()<="я" or ch in "ё")
    lat=sum(1 for ch in text if "a"<=ch.lower()<="z")
    if cyr>lat: return "ru"
    if lat>cyr: return "en"
    return DEFAULT_LANG

def mood_of(text):
    t=text.lower()
    if any(w in t for w in ["вымот","устал","тяжело","плохо","груст","печаль","тревог","пережив"]): return "tired"
    if any(w in t for w in ["класс","супер","кайф","рад","ура","огонь"]): return "happy"
    if any(w in t for w in ["злю","бесит","раздраж","злость"]): return "angry"
    return "neutral"

def empathy_reply(text, mood, style):
    if mood=="tired": 
        if style=="professor": return "Понимаю. Сделай паузу на пару минут. Хочешь, дам короткий план, как распределить силы?"
        if style=="sarcastic": return "Кофе не завезли? Окей, давай разгребём это по-быстрому."
        return "Понимаю. Давай упростим задачу. Могу подсказать плейлист или дыхательную технику."
    if mood=="happy":
        if style=="professor": return "Отличные новости. Давай зафиксируем, что сработало, чтобы повторить успех."
        if style=="sarcastic": return "Ну вот, мир не так уж и плох. Что дальше покоряем?"
        return "Круто! Поехали дальше — я рядом."
    if mood=="angry":
        if style=="professor": return "Понимаю раздражение. Предлагаю разложить проблему на части и решить по порядку."
        if style=="sarcastic": return "Окей, выпускаем пар и превращаем хаос в план."
        return "Я с тобой. Давай спокойно разберёмся и найдём решение."
    return ""

def sys_persona(style):
    if style=="professor": return "Отвечай подробно, структурировано, с примерами и правилами. Будь доброжелателен."
    if style=="sarcastic": return "Отвечай кратко, с лёгкой ироничной самоиронией, но без грубости."
    return "Отвечай чётко, по делу, дружелюбно, адаптируй язык к пользователю."

async def openai_chat(messages, lang="ru"):
    url="https://api.openai.com/v1/chat/completions"
    payload={"model":OPENAI_MODEL,"messages":messages,"temperature":0.4}
    async with httpx.AsyncClient(timeout=120) as cl:
        r=await cl.post(url, headers={"Authorization":f"Bearer {OPENAI_KEY}","Content-Type":"application/json"}, json=payload)
        r.raise_for_status()
        data=r.json()
        return data["choices"][0]["message"]["content"].strip()

async def tts_mp3(text, voice="alloy"):
    url="https://api.openai.com/v1/audio/speech"
    payload={"model":"gpt-4o-mini-tts","voice":voice,"input":text}
    async with httpx.AsyncClient(timeout=None) as cl:
        r=await cl.post(url, headers={"Authorization":f"Bearer {OPENAI_KEY}","Content-Type":"application/json"}, json=payload)
        r.raise_for_status()
        return r.content

async def stt_text(voice_bytes:bytes):
    url="https://api.openai.com/v1/audio/transcriptions"
    form=aiohttp.FormData()
    form.add_field("model","whisper-1")
    form.add_field("file", voice_bytes, filename="audio.ogg", content_type="audio/ogg")
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=form, headers={"Authorization":f"Bearer {OPENAI_KEY}"}) as r:
            if r.status!=200:
                t=await r.text()
                raise RuntimeError(t)
            data=await r.json()
            return data.get("text","").strip()

async def read_url(url):
    r=await http_get(url,30)
    doc=Document(r.text)
    html=doc.summary()
    soup=BeautifulSoup(html,"lxml")
    text=soup.get_text("\n")
    text=re.sub(r"\n{3,}","\n\n",text)
    return text[:8000]

async def read_pdf_bytes(b:bytes):
    buf=io.BytesIO(b)
    txt=pdf_extract_text(buf) or ""
    return txt[:8000]

async def read_docx_bytes(b:bytes):
    buf=io.BytesIO(b)
    d=DocxDocument(buf)
    paras=[p.text for p in d.paragraphs]
    return ("\n".join(paras))[:8000]

def parse_translate_prefix(s):
    m=re.match(r"^\s*(?:/translate|translate|переведи|>>|->)\s*(to)?\s*([a-zA-Z\-]{2,})\s*[:\-]?\s*", s, re.I)
    if m:
        tgt=m.group(2).lower()
        rest=s[m.end():].strip()
        return tgt, rest
    return None, s

async def plan_tools(u, text):
    need_web=ALWAYS_WEB or u["web_mode"]=="always"
    if u["web_mode"]=="off": need_web=False
    if not need_web and any(k in text.lower() for k in ["http://","https://",".pdf",".docx","новости","курс","погода","сколько стоит","что происходит","сводка дня"]): need_web=True
    return {"web":need_web}

async def run_tools(toolplan, text):
    out=[]
    if "http://" in text or "https://" in text:
        urls=re.findall(r"(https?://\S+)", text)[:3]
        for u in urls:
            try:
                if u.lower().endswith(".pdf"):
                    r=await http_get(u,30); out.append(await read_pdf_bytes(r.content))
                elif u.lower().endswith(".docx"):
                    r=await http_get(u,30); out.append(await read_docx_bytes(r.content))
                else:
                    out.append(await read_url(u))
            except:
                pass
    if "погода" in text.lower():
        m=re.search(r"погод[аеы]\s+в\s+([A-Za-zА-Яа-яёЁ\-\s]{2,})", text, re.I)
        city=m.group(1).strip() if m else ""
        if city:
            try: out.append(await fetch_weather(city))
            except: pass
    if "курс" in text.lower():
        m=re.search(r"курс\s+([a-z]{3})", text, re.I)
        cur=m.group(1) if m else "usd"
        try: out.append(await fetch_currency(cur))
        except: pass
    if "новост" in text.lower():
        try: out.append(await ddg_news("top news",5))
        except: pass
    if toolplan.get("web"):
        try:
            with DDGS() as d:
                hits=d.text(text, max_results=5, region="ru-ru")
            urls=[h["href"] for h in hits if "href" in h][:3]
            for u in urls:
                try: out.append(await read_url(u))
                except: pass
        except:
            pass
    return "\n\n".join([o for o in out if o])[:8000]

async def build_reply(u, text, files_context=""):
    mood=mood_of(text)
    emp=empathy_reply(text, mood, u["personality"])
    sysmsg=sys_persona(u["personality"])
    tgt, stripped=parse_translate_prefix(text)
    target=tgt or None
    if target:
        prompt=[{"role":"system","content":f"Ты переводчик. Переведи на язык {target}. Сохраняй смысл и тон."}]
        prompt.append({"role":"user","content":stripped})
        reply=await openai_chat(prompt, u["lang"])
        return reply, "translate", target, emp
    tools=await plan_tools(u, text)
    toolctx=await run_tools(tools, text)
    mem=await get_memory(u["user_id"])
    msgs=[{"role":"system","content":sysmsg}]
    for m in mem[-12:]:
        msgs.append(m)
    if files_context:
        msgs.append({"role":"user","content":f"Контент из файлов/ссылок:\n{files_context}\n\nТеперь ответь на запрос пользователя ниже."})
    msgs.append({"role":"user","content":text})
    if toolctx:
        msgs.append({"role":"system","content":"Внешние сведения:\n"+toolctx})
    reply=await openai_chat(msgs, u["lang"])
    return (emp+"\n\n" if emp else "")+reply, "normal", None, emp

async def handle_files(update:Update, context:ContextTypes.DEFAULT_TYPE):
    texts=[]
    for a in update.message.document,:
        pass
    for doc in update.message.documents or []:
        fid=doc.file_id
        f=await context.bot.get_file(fid)
        b=await f.download_as_bytearray()
        if doc.mime_type and "pdf" in doc.mime_type.lower():
            try: texts.append(await read_pdf_bytes(bytes(b)))
            except: pass
        elif doc.mime_type and ("word" in doc.mime_type.lower() or doc.file_name.lower().endswith(".docx")):
            try: texts.append(await read_docx_bytes(bytes(b)))
            except: pass
        else:
            try:
                if doc.file_name.lower().endswith(".csv"):
                    texts.append(bytes(b).decode(errors="ignore")[:8000])
            except: pass
    return "\n\n".join(texts)

async def cmd_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    await update.message.reply_text("Привет, я Jarvis v2 Ultimate 🤖", reply_markup=main_menu())

async def cmd_reset(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    c=await db_conn(); await c.execute("delete from memory where user_id=$1", uid); await c.close()
    await update.message.reply_text("Память очищена.")

async def on_text(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    text=update.message.text or ""
    files_ctx=""
    if update.message.entities:
        urls=[text[e.offset:e.offset+e.length] for e in update.message.entities if e.type in ["url","text_link"]]
        for url in urls[:3]:
            try: files_ctx+=await read_url(url)+"\n\n"
            except: pass
    reply, mode, target, emp=await build_reply(u, text, files_ctx)
    await add_memory(uid,"user",text)
    await add_memory(uid,"assistant",reply)
    await trim_memory(uid)
    await update.message.reply_text(reply)

async def on_voice(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    v=update.message.voice or update.message.audio
    if not v:
        await update.message.reply_text("Не удалось получить аудио.")
        return
    f=await context.bot.get_file(v.file_id)
    b=await f.download_as_bytearray()
    text=await stt_text(bytes(b))
    files_ctx=""
    reply, mode, target, emp=await build_reply(u, text, files_ctx)
    await add_memory(uid,"user",text)
    await add_memory(uid,"assistant",reply)
    await trim_memory(uid)
    if u["tts_enabled"]:
        mp3=await tts_mp3(reply, u["voice"])
        await update.message.reply_voice(InputFile(io.BytesIO(mp3),"reply.mp3"), caption=None)
    else:
        await update.message.reply_text(reply)

async def on_doc(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    files_ctx=await handle_files(update, context)
    text=update.message.caption or "Проанализируй файл и сделай выводы."
    reply, mode, target, emp=await build_reply(u, text, files_ctx)
    await add_memory(uid,"user",text)
    await add_memory(uid,"assistant",reply)
    await trim_memory(uid)
    await update.message.reply_text(reply)

async def on_photo(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    text=update.message.caption or "Опиши изображение."
    reply, mode, target, emp=await build_reply(u, text, "")
    await add_memory(uid,"user",text)
    await add_memory(uid,"assistant",reply)
    await trim_memory(uid)
    await update.message.reply_text(reply)

async def cbq(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    uid=q.from_user.id
    u=await get_user(uid)
    data=q.data or ""
    if data=="weather":
        await q.answer()
        await q.edit_message_text("Пришли: погода в <город>")
        return
    if data=="currency":
        await q.answer()
        await q.edit_message_text("Пришли: курс <валюта>, например курс usd")
        return
    if data=="news":
        await q.answer()
        n=await ddg_news("top news",5)
        await q.edit_message_text(n, reply_markup=main_menu())
        return
    if data=="fact":
        await q.answer()
        f=await random_fact()
        await q.edit_message_text(f, reply_markup=main_menu())
        return
    if data=="settings":
        await q.answer()
        await q.edit_message_text("Настройки:", reply_markup=settings_menu(u))
        return
    if data=="back_home":
        await q.answer()
        await q.edit_message_text("Главное меню:", reply_markup=main_menu())
        return
    if data=="toggle_tts":
        u["tts_enabled"]=not u["tts_enabled"]; await save_user(u)
        await q.answer("Ок")
        await q.edit_message_reply_markup(reply_markup=settings_menu(u))
        return
    if data=="set_voice":
        voices=["alloy","verse","aria","luna","sage"]
        i=(voices.index(u["voice"]) if u["voice"] in voices else -1)+1
        u["voice"]=voices[i%len(voices)]; await save_user(u)
        await q.answer("Ок")
        await q.edit_message_reply_markup(reply_markup=settings_menu(u))
        return
    if data=="set_lang":
        langs=["ru","en","uk","de","fr","es","it","tr","kk"]
        i=(langs.index(u["lang"]) if u["lang"] in langs else -1)+1
        u["lang"]=langs[i%len(langs)]; await save_user(u)
        await q.answer("Ок")
        await q.edit_message_reply_markup(reply_markup=settings_menu(u))
        return
    if data=="set_personality":
        opts=["assistant","professor","sarcastic"]
        i=(opts.index(u["personality"]) if u["personality"] in opts else -1)+1
        u["personality"]=opts[i%len(opts)]; await save_user(u)
        await q.answer("Ок")
        await q.edit_message_reply_markup(reply_markup=settings_menu(u))
        return
    if data=="set_webmode":
        opts=["off","auto","always"]
        i=(opts.index(u["web_mode"]) if u["web_mode"] in opts else -1)+1
        u["web_mode"]=opts[i%len(opts)]; await save_user(u)
        await q.answer("Ок")
        await q.edit_message_reply_markup(reply_markup=settings_menu(u))
        return
    if data=="set_translate_to":
        opts=["en","ru","de","fr","es","it","tr","kk"]
        i=(opts.index(u["translate_to"]) if u["translate_to"] in opts else -1)+1
        u["translate_to"]=opts[i%len(opts)]; await save_user(u)
        await q.answer("Ок")
        await q.edit_message_reply_markup(reply_markup=settings_menu(u))
        return
    await q.answer()

async def cmd_weather(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args=update.message.text.split(maxsplit=1)
    if len(args)<2:
        await update.message.reply_text("Использование: /weather <город>")
        return
    w=await fetch_weather(args[1])
    await update.message.reply_text(w)

async def cmd_currency(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args=update.message.text.split(maxsplit=1)
    base="usd" if len(args)<2 else args[1]
    c=await fetch_currency(base)
    await update.message.reply_text(c)

async def cmd_news(update:Update, context:ContextTypes.DEFAULT_TYPE):
    n=await ddg_news("top news",5)
    await update.message.reply_text(n)

async def cmd_fact(update:Update, context:ContextTypes.DEFAULT_TYPE):
    f=await random_fact()
    await update.message.reply_text(f)

async def cmd_personality(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    args=update.message.text.split(maxsplit=1)
    if len(args)<2:
        await update.message.reply_text(f"Сейчас: {u['personality']}. Варианты: assistant, professor, sarcastic")
        return
    u["personality"]=args[1].strip().lower()
    await save_user(u)
    await update.message.reply_text("Готово.")

async def cmd_toggle_tts(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    u["tts_enabled"]=not u["tts_enabled"]; await save_user(u)
    await update.message.reply_text(f"Озвучка: {'вкл' if u['tts_enabled'] else 'выкл'}")

async def cmd_setlang(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    args=update.message.text.split(maxsplit=1)
    if len(args)<2:
        await update.message.reply_text(f"Сейчас: {u['lang']}. Пример: /setlang en")
        return
    u["lang"]=args[1].strip().lower(); await save_user(u)
    await update.message.reply_text("Готово.")

async def cmd_setvoice(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    args=update.message.text.split(maxsplit=1)
    if len(args)<2:
        await update.message.reply_text(f"Сейчас: {u['voice']}. Пример: /setvoice alloy")
        return
    u["voice"]=args[1].strip().lower(); await save_user(u)
    await update.message.reply_text("Готово.")

async def cmd_translate(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    args=update.message.text.split(maxsplit=2)
    if len(args)<3:
        await update.message.reply_text("Использование: /translate <to-lang> <текст>")
        return
    tgt=args[1].lower(); src=args[2]
    msg=[{"role":"system","content":f"Ты переводчик. Переведи на язык {tgt}. Сохраняй смысл и тон."},{"role":"user","content":src}]
    out=await openai_chat(msg, u["lang"])
    await update.message.reply_text(out)

async def tg_webhook(request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="bad json", status=400)
    upd = Update.de_json(data, application.bot)
    request.app.loop.create_task(application.process_update(upd))
    return web.Response(text="ok")

async def health(request):
    return web.Response(text="ok")

def build_app():
    app=ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("currency", cmd_currency))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("fact", cmd_fact))
    app.add_handler(CommandHandler("personality", cmd_personality))
    app.add_handler(CommandHandler("toggletts", cmd_toggle_tts))
    app.add_handler(CommandHandler("setlang", cmd_setlang))
    app.add_handler(CommandHandler("setvoice", cmd_setvoice))
    app.add_handler(CommandHandler("translate", cmd_translate))
    app.add_handler(CallbackQueryHandler(cbq))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, on_doc))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

async def start_http():
    global application
    await init_db()
    application=build_app()
    await application.initialize()
    await application.start()
    aio=web.Application()
    aio.add_routes([web.get("/health", health)])
    aio.add_routes([web.post("/tgwebhook", tg_webhook)])
    runner=web.AppRunner(aio)
    await runner.setup()
    site=web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)
    return aio

async def main():
    await start_http()
    await asyncio.Event().wait()

def run():
    loop=asyncio.get_event_loop()
    aio_app=loop.run_until_complete(start_http())
    web.run_app(aio_app, host="0.0.0.0", port=PORT)

if __name__=="__main__":
    try:
        asyncio.run(main())
    except RuntimeError:
        run()
