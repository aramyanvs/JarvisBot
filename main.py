import os
import io
import re
import json
import asyncio
import time
import tempfile
import uuid
import logging
from pathlib import Path
from urllib.parse import urlparse
import httpx
import asyncpg
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
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
OPENAI_API_KEY=os.getenv("OPENAI_API_KEY","")
OPENAI_MODEL=os.getenv("OPENAI_MODEL","gpt-4o-mini")
BASE_URL=os.getenv("BASE_URL","").rstrip("/")
DB_URL=os.getenv("DB_URL","")
ALWAYS_WEB=os.getenv("ALWAYS_WEB","true").lower()=="true"
LANG=os.getenv("LANGUAGE","ru")
MEM_LIMIT=int(os.getenv("MEMORY_LIMIT","1500"))
VOICE_MODE=os.getenv("VOICE_MODE","true").lower()=="true"
PORT=int(os.getenv("PORT","8080"))

logging.basicConfig(level=logging.INFO)
log=logging.getLogger("jarvis")

aclient=AsyncOpenAI(api_key=OPENAI_API_KEY,timeout=60)
application: Optional[Application]=None

ALLOWED_SCHEMES={"http","https"}
BLOCKED_PREFIXES=("localhost","127.","0.","10.","192.168.","172.16.","172.17.","172.18.","172.19.","172.2","169.254.")

def safe_url(url:str)->bool:
    try:
        u=urlparse(url)
        if u.scheme not in ALLOWED_SCHEMES: return False
        h=(u.hostname or "").lower()
        return not h.startswith(BLOCKED_PREFIXES)
    except Exception:
        return False

async def db_conn():
    return await asyncpg.connect(DB_URL)

async def init_db():
    c=await db_conn()
    await c.execute(f"create table if not exists users (user_id bigint primary key, lang text default '{LANG}', persona text default 'assistant', voice boolean default true, translate_to text default null, voicetrans boolean default false)")
    await c.execute("create table if not exists memory (user_id bigint references users(user_id) on delete cascade, role text, content text, ts timestamptz default now())")
    await c.close()

async def get_user(uid:int)->dict:
    c=await db_conn()
    row=await c.fetchrow("select user_id,lang,persona,voice,translate_to,voicetrans from users where user_id=$1",uid)
    if not row:
        await c.execute("insert into users(user_id) values($1)",uid)
        row=await c.fetchrow("select user_id,lang,persona,voice,translate_to,voicetrans from users where user_id=$1",uid)
    await c.close()
    d=dict(row)
    return {"user_id":d["user_id"],"lang":d["lang"],"persona":d["persona"],"voice":d["voice"],"translate_to":d["translate_to"],"voicetrans":d["voicetrans"]}

async def set_user(uid:int,**kw):
    if not kw: return
    fields=[]
    vals=[]
    for k,v in kw.items():
        fields.append(f"{k}=${len(vals)+1}")
        vals.append(v)
    vals.append(uid)
    c=await db_conn()
    await c.execute("update users set "+", ".join(fields)+" where user_id=$"+str(len(vals)),*vals)
    await c.close()

def _clip_messages(hist:List[Dict[str,str]])->List[Dict[str,str]]:
    total=0
    out=[]
    for m in reversed(hist):
        total+=len(m.get("content",""))
        out.append(m)
        if total>MEM_LIMIT: break
    return list(reversed(out))

async def get_memory(uid:int)->List[Dict[str,str]]:
    c=await db_conn()
    rows=await c.fetch("select role,content from memory where user_id=$1 order by ts asc",uid)
    await c.close()
    hist=[{"role":r["role"],"content":r["content"]} for r in rows]
    return _clip_messages(hist)

async def add_memory(uid:int,role:str,content:str):
    c=await db_conn()
    await c.execute("insert into memory(user_id,role,content) values($1,$2,$3)",uid,role,content)
    await c.close()

async def reset_memory(uid:int):
    c=await db_conn()
    await c.execute("delete from memory where user_id=$1",uid)
    await c.close()

def sys_prompt(persona:str,lang:str)->str:
    base="Отвечай кратко и по делу."
    if persona=="professor": base="Объясняй подробно, по шагам, с примерами."
    if persona=="sarcastic": base="Отвечай с лёгкой иронией, но оставайся полезным."
    return f"{base} Язык ответа: {lang}. При наличии свежих ссылок используй веб-контент."

def guess_lang(text:str)->str:
    return "ru" if re.search(r"[А-Яа-яЁё]",text) else "en"

async def empathize(text:str,lang:str)->str:
    try:
        r=await aclient.chat.completions.create(model=OPENAI_MODEL,messages=[{"role":"system","content":"Определи настроение пользователя: neutral, positive, stressed, sad, angry. Верни одно слово."},{"role":"user","content":text}],temperature=0.2,max_tokens=5)
        mood=(r.choices[0].message.content or "neutral").strip().lower()
    except Exception:
        mood="neutral"
    if lang.startswith("ru"):
        m={"positive":"Рад это слышать!","stressed":"Понимаю. Я рядом.","sad":"Сочувствую.","angry":"Понимаю. Давай решим.","neutral":"Принято."}
    else:
        m={"positive":"Glad to hear!","stressed":"I get it.","sad":"Sorry to hear.","angry":"Understood.","neutral":"Got it."}
    return m.get(mood,"Got it.")

async def ddg_search(q:str,k:int=5)->List[Dict[str,str]]:
    out=[]
    try:
        with DDGS(timeout=15) as dd:
            for r in dd.text(q,max_results=k):
                out.append({"title":r.get("title",""),"href":r.get("href",""),"body":r.get("body","")})
    except Exception:
        pass
    return out

async def fetch_url(url:str)->str:
    if not safe_url(url): return ""
    async with httpx.AsyncClient(timeout=30,follow_redirects=True,headers={"User-Agent":"JarvisBot/1.0"}) as x:
        r=await x.get(url)
        html=r.text
    doc=Document(html)
    cleaned=doc.summary()
    cleaner=Cleaner(style=True,scripts=True,comments=True,links=False,meta=False,page_structure=False,processing_instructions=True,embedded=True,frames=True,forms=True,annoying_tags=True,remove_unknown_tags=False)
    cleaned=cleaner.clean_html(cleaned)
    soup=BeautifulSoup(cleaned,"html.parser")
    text=" ".join(soup.get_text(" ").split())
    return text[:12000]

async def web_context(query:str)->str:
    try:
        results=await ddg_search(query,5)
        chunks=[]
        for r in results[:3]:
            u=r.get("href") or ""
            if not u.startswith("http"): continue
            try:
                t=await fetch_url(u)
                if t: chunks.append(f"{r.get('title','')}\n{u}\n{t}\n")
            except Exception:
                continue
        return "\n\n".join(chunks)[:20000]
    except Exception:
        return ""

async def weather(city:str)->str:
    url=f"https://wttr.in/{city}?format=j1"
    async with httpx.AsyncClient(timeout=20) as x:
        j=(await x.get(url)).json()
    cur=j["current_condition"][0]
    area=j["nearest_area"][0]["areaName"][0]["value"]
    return f"{area}: {cur['temp_C']}°C (ощущается {cur['FeelsLikeC']}°C), {cur['weatherDesc'][0]['value']}"

async def currency(base:str="USD",symbols:str="RUB,EUR")->str:
    url=f"https://api.exchangerate.host/latest?base={base.upper()}&symbols={symbols.upper()}"
    async with httpx.AsyncClient(timeout=20) as x:
        j=(await x.get(url)).json()
    rates=j.get("rates",{}) or {}
    if not rates: return "N/A"
    return "\n".join([f"1 {base.upper()} = {rates[k]:.4f} {k}" for k in rates])

async def latest_news(q:str="world")->str:
    res=await ddg_search(q,6)
    picks=[]
    for r in res[:5]:
        u=r.get("href","")
        if not u.startswith("http"): continue
        try:
            txt=await fetch_url(u)
        except Exception:
            continue
        picks.append({"title":r.get("title",""),"url":u,"text":txt[:3000]})
    if not picks: return "Нет новостей."
    body="\n\n".join([f"{i+1}. {p['title']}\n{p['url']}" for i,p in enumerate(picks)])
    try:
        s=await aclient.chat.completions.create(model=OPENAI_MODEL,messages=[{"role":"system","content":"Суммируй пункты кратко списком."},{"role":"user","content":body}],temperature=0.3,max_tokens=500)
        return s.choices[0].message.content or body
    except Exception:
        return body

async def random_fact()->str:
    res=await ddg_search("interesting facts today",5)
    for r in res:
        u=r.get("href","")
        if not u.startswith("http"): continue
        try:
            t=await fetch_url(u)
            s=await aclient.chat.completions.create(model=OPENAI_MODEL,messages=[{"role":"system","content":"Выдели один интересный факт, одной фразой."},{"role":"user","content":t[:8000]}],temperature=0.7,max_tokens=120)
            return s.choices[0].message.content or "Факт не найден."
        except Exception:
            continue
    return "Факт не найден."

async def llm(messages:List[Dict[str,str]],sys:str)->str:
    r=await aclient.chat.completions.create(model=OPENAI_MODEL,messages=[{"role":"system","content":sys}]+messages,temperature=0.6,max_tokens=1000)
    return r.choices[0].message.content

async def to_tts(text:str,voice:str="alloy")->bytes:
    r=await aclient.audio.speech.create(model="gpt-4o-mini-tts",voice=voice,input=text)
    b=await r.get_reader().read()
    return b

async def transcribe(file_path:str)->str:
    with open(file_path,"rb") as f:
        r=await aclient.audio.transcriptions.create(model="whisper-1",file=f,language="auto")
    return r.text

async def translate_text(text:str,to_lang:str)->str:
    r=await aclient.chat.completions.create(model=OPENAI_MODEL,messages=[{"role":"system","content":f"Переведи текст на {to_lang} кратко, без лишних комментариев."},{"role":"user","content":text}],temperature=0.2,max_tokens=1000)
    return r.choices[0].message.content

async def summarize_text(text:str,lang:str)->str:
    r=await aclient.chat.completions.create(model=OPENAI_MODEL,messages=[{"role":"system","content":f"Суммируй кратко на {lang}."},{"role":"user","content":text}],temperature=0.3,max_tokens=600)
    return r.choices[0].message.content

async def openai_image(prompt:str)->bytes:
    im=await aclient.images.generate(model="gpt-image-1",prompt=prompt,size="1024x1024",response_format="b64_json")
    import base64
    return base64.b64decode(im.data[0].b64_json)

async def parse_file(file_path:str,file_name:str)->str:
    n=file_name.lower()
    if n.endswith(".pdf"):
        return pdf_extract_text(file_path)[:20000]
    if n.endswith(".docx"):
        d=DocxDocument(file_path)
        return "\n".join([p.text for p in d.paragraphs])[:20000]
    if n.endswith(".csv"):
        df=pd.read_csv(file_path)
        return df.to_markdown()[:20000]
    if n.endswith(".xlsx") or n.endswith(".xls"):
        df=pd.read_excel(file_path)
        return df.to_markdown()[:20000]
    with open(file_path,"r",errors="ignore") as f:
        return f.read()[:20000]

def build_bot()->Application:
    return ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()

async def cmd_start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    await get_user(uid)
    txt="Привет! Я Jarvis. Доступно: /weather <город>, /currency <база> [символы], /news [запрос], /fact, /reset, /setlang <ru|en|...>, /personality <assistant|professor|sarcastic>, /voicetrans <on|off>, /image <промпт>, /stats. Пиши или пришли голос."
    await update.message.reply_text(txt)

async def cmd_reset(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    await reset_memory(uid)
    await update.message.reply_text("Окей, контекст очищен.")

async def cmd_setlang(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    if not context.args:
        await update.message.reply_text("Пример: /setlang ru")
        return
    lang=context.args[0].lower()
    await set_user(uid,lang=lang)
    await update.message.reply_text(f"Язык по умолчанию: {lang}")

async def cmd_personality(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    if not context.args:
        await update.message.reply_text("Варианты: assistant, professor, sarcastic")
        return
    p=context.args[0].lower()
    if p not in ["assistant","professor","sarcastic"]:
        await update.message.reply_text("Неверно. Варианты: assistant, professor, sarcastic")
        return
    await set_user(uid,persona=p)
    await update.message.reply_text(f"Персональность: {p}")

async def cmd_voicetrans(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    if not context.args:
        await update.message.reply_text("Использование: /voicetrans on|off")
        return
    on=context.args[0].lower() in ["on","1","true","yes"]
    await set_user(uid,voicetrans=on)
    await update.message.reply_text("Перевод voice: "+("включён" if on else "выключен"))

async def cmd_weather(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /weather Moscow")
        return
    city=" ".join(context.args)
    try:
        w=await weather(city)
        await update.message.reply_text(w)
    except Exception:
        await update.message.reply_text("Не удалось получить погоду.")

async def cmd_currency(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /currency usd rub,eur")
        return
    base=context.args[0]
    syms=context.args[1] if len(context.args)>1 else "RUB,EUR"
    try:
        r=await currency(base,syms.upper())
        await update.message.reply_text(r)
    except Exception:
        await update.message.reply_text("Не удалось получить курсы.")

async def cmd_news(update:Update,context:ContextTypes.DEFAULT_TYPE):
    q=" ".join(context.args) if context.args else "world news today"
    try:
        s=await latest_news(q)
        await update.message.reply_text(s)
    except Exception:
        await update.message.reply_text("Новости недоступны.")

async def cmd_fact(update:Update,context:ContextTypes.DEFAULT_TYPE):
    try:
        f=await random_fact()
        await update.message.reply_text(f)
    except Exception:
        await update.message.reply_text("Факт недоступен.")

async def cmd_image(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /image astronaut cat in neon city")
        return
    prompt=" ".join(context.args)
    try:
        img=await openai_image(prompt)
        await update.message.reply_photo(photo=img,filename=f"image_{uuid.uuid4().hex}.png")
    except Exception:
        await update.message.reply_text("Не удалось сгенерировать изображение.")

async def cmd_stats(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    c=await db_conn()
    row=await c.fetchrow("select coalesce(sum(length(content)),0) as s from memory where user_id=$1",uid)
    await c.close()
    used=row["s"] or 0
    await update.message.reply_text(f"📊 Вы использовали ~{used} символов памяти.")

async def on_text(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    text=update.message.text or ""
    lang=u["lang"] or guess_lang(text)
    mood=await empathize(text,lang)
    hist=await get_memory(uid)
    if ALWAYS_WEB or re.search(r"https?://|новост|news|ссылк|прочитай|итог|resume|summar",text,re.I):
        webtxt=await web_context(text)
        if webtxt:
            hist.append({"role":"system","content":"Веб-контент:\n"+webtxt})
    sys=sys_prompt(u["persona"],lang)
    hist2=hist+[{"role":"user","content":text}]
    try:
        reply=await llm(hist2,sys)
    except Exception:
        reply="Проблема с моделью."
    await add_memory(uid,"user",text)
    await add_memory(uid,"assistant",reply)
    await update.message.reply_text((mood+"\n\n"+reply).strip())

async def on_document(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    doc=update.message.document
    f=await doc.get_file()
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        await f.download_to_drive(tmp.name)
        content=await parse_file(tmp.name,doc.file_name or "file")
    s=await summarize_text(content[:18000],u["lang"])
    await add_memory(uid,"user","[файл загружен]")
    await add_memory(uid,"assistant",s)
    await update.message.reply_text(s)

async def on_voice(update:Update,context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    v=update.message.voice or update.message.audio
    if not v:
        await update.message.reply_text("Голос не найден.")
        return
    f=await v.get_file()
    with tempfile.NamedTemporaryFile(suffix=".oga",delete=False) as tmp:
        await f.download_to_drive(tmp.name)
        try:
            text=await transcribe(tmp.name)
        except Exception:
            await update.message.reply_text("Не удалось распознать голос.")
            return
    lang=u["lang"] or guess_lang(text)
    hist=await get_memory(uid)
    if u["voicetrans"] and u["translate_to"]:
        try:
            reply=await translate_text(text,u["translate_to"])
        except Exception:
            reply="Не удалось перевести."
    else:
        if ALWAYS_WEB or re.search(r"https?://|новост|news|ссылк|прочитай|итог|resume|summar",text,re.I):
            webtxt=await web_context(text)
            if webtxt:
                hist.append({"role":"system","content":"Веб-контент:\n"+webtxt})
        sys=sys_prompt(u["persona"],lang)
        hist2=hist+[{"role":"user","content":text}]
        try:
            reply=await llm(hist2,sys)
        except Exception:
            reply="Проблема с моделью."
    await add_memory(uid,"user",text)
    await add_memory(uid,"assistant",reply)
    if VOICE_MODE:
        try:
            audio=await to_tts(reply,"alloy")
            await update.message.reply_voice(voice=audio)
        except Exception:
            await update.message.reply_text(reply)
    else:
        await update.message.reply_text(reply)

async def tg_webhook(request):
    try:
        data=await request.json()
    except Exception:
        return web.Response(text="ok")
    try:
        upd=Update.de_json(data,application.bot)
        asyncio.create_task(application.process_update(upd))
    except Exception:
        pass
    return web.Response(text="ok")

async def health(request):
    return web.Response(text="ok")

def routes_app():
    app=web.Application()
    app.router.add_get("/",health)
    app.router.add_get("/health",health)
    app.router.add_post("/tgwebhook",tg_webhook)
    return app

def add_handlers(app:Application):
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("reset",cmd_reset))
    app.add_handler(CommandHandler("setlang",cmd_setlang))
    app.add_handler(CommandHandler("personality",cmd_personality))
    app.add_handler(CommandHandler("voicetrans",cmd_voicetrans))
    app.add_handler(CommandHandler("weather",cmd_weather))
    app.add_handler(CommandHandler("currency",cmd_currency))
    app.add_handler(CommandHandler("news",cmd_news))
    app.add_handler(CommandHandler("fact",cmd_fact))
    app.add_handler(CommandHandler("image",cmd_image))
    app.add_handler(CommandHandler("stats",cmd_stats))
    app.add_handler(MessageHandler(filters.Document.ALL,on_document))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO,on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,on_text))

async def start_http():
    global application
    await init_db()
    if application is None:
        application=build_bot()
        add_handlers(application)
        await application.initialize()
        await application.start()
    aio=routes_app()
    runner=web.AppRunner(aio)
    await runner.setup()
    site=web.TCPSite(runner,"0.0.0.0",PORT)
    await site.start()
    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL}/tgwebhook",drop_pending_updates=True)
    return aio

async def main():
    await start_http()
    await asyncio.Event().wait()

def run():
    asyncio.run(main())

if __name__=="__main__":
    run()
