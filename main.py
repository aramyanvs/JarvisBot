import os, asyncio, io, re, tempfile
from dotenv import load_dotenv
load_dotenv()

import asyncpg, httpx, pandas as pd
from readability import Document
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
from openai import OpenAI
from pdfminer.high_level import extract_text as pdf_text
from docx import Document as Docx
from aiohttp import web

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

OPENAI_KEY=os.getenv("OPENAI_API_KEY")
DB_URL=os.getenv("DB_URL")
BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID=int(os.getenv("ADMIN_ID","0"))
MODEL=os.getenv("OPENAI_MODEL","gpt-4o")
MEM_LIMIT=int(os.getenv("MEMORY_LIMIT","1500"))
LANG=os.getenv("LANGUAGE","ru")
UA="Mozilla/5.0"
SYS=f"Ты Jarvis — ассистент на {LANG}. Отвечай кратко и по делу. Если нужна свежая информация, используй сводку, приложенную в system."
PORT=int(os.getenv("PORT","10000"))
BASE_URL=os.getenv("PUBLIC_URL","").rstrip("/")

oc=OpenAI(api_key=OPENAI_KEY)
application: Application|None=None

async def db_conn(): return await asyncpg.connect(DB_URL)
async def init_db():
    c=await db_conn()
    await c.execute("create table if not exists users (user_id bigint primary key, memory jsonb default '[]'::jsonb)")
    await c.close()
async def get_memory(uid:int):
    c=await db_conn()
    r=await c.fetchrow("select memory from users where user_id=$1", uid)
    await c.close()
    return r["memory"] if r else []
async def save_memory(uid:int, mem):
    c=await db_conn()
    await c.execute("insert into users(user_id,memory) values($1,$2) on conflict(user_id) do update set memory=excluded.memory", uid, mem)
    await c.close()

def ask_openai(messages, temperature=0.3, max_tokens=800):
    r=oc.chat.completions.create(model=MODEL, messages=messages, temperature=temperature, max_tokens=max_tokens)
    return r.choices[0].message.content.strip()

async def fetch_url(url:str, limit=20000):
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent":UA}, timeout=25) as cl:
        r=await cl.get(url)
    ct=r.headers.get("content-type","").lower()
    if "text/html" in ct or "<html" in r.text[:500].lower():
        doc=Document(r.text); html=doc.summary()
        soup=BeautifulSoup(html,"lxml"); text=soup.get_text("\n", strip=True)
    else:
        text=r.text
    return re.sub(r"\n{3,}", "\n\n", text)[:limit]

def need_web(q:str):
    t=q.lower()
    keys=["сейчас","сегодня","новост","курс","цена","сколько стоит","когда будет","последн","обнов","релиз","погода","расписан","матч","акции","доступно","вышел","итог"]
    if any(k in t for k in keys): return True
    if re.search(r"\b20(2[4-9]|3\d)\b", t): return True
    if "http://" in t or "https://" in t: return True
    return False

def extract_urls(q:str): return re.findall(r"https?://\S+", q)

async def fetch_urls(urls, limit_chars=12000):
    out=[]
    for u in urls[:3]:
        try:
            t=await fetch_url(u, limit=4000)
            if t: out.append(t)
        except: pass
    return "\n\n".join(out)[:limit_chars]

async def search_and_fetch(query:str, hits:int=2, limit_chars:int=12000):
    links=[]
    with DDGS() as ddg:
        for r in ddg.text(query, max_results=hits, safesearch="moderate"):
            if r and r.get("href"): links.append(r["href"])
    if not links: return ""
    return await fetch_urls(links, limit_chars=limit_chars)

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

async def set_menu(app:Application):
    await app.bot.set_my_commands([
        BotCommand("start","Запуск"),
        BotCommand("ping","Проверка"),
        BotCommand("read","Прочитать сайт"),
        BotCommand("reset","Сбросить память"),
    ])

async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await set_menu(ctx.application)
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Начать", callback_data="start")]])
    await update.message.reply_text("Готов. Меню установлено. Пиши вопрос или нажми кнопку.", reply_markup=kb)

async def on_button(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()
    if q.data=="start":
        await q.edit_message_text("Готов. Пиши вопрос.")

async def cmd_ping(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_reset(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await save_memory(update.effective_user.id, [])
    await update.message.reply_text("Память очищена.")

async def cmd_read(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    if len(parts)<2: return await update.message.reply_text("Формат: /read URL")
    try:
        raw=await fetch_url(parts[1])
    except Exception as e:
        return await update.message.reply_text(f"Ошибка: {e}")
    sys=[{"role":"system","content":"Суммаризируй текст кратко и структурировано."}]
    out=ask_openai(sys+[{"role":"user","content":raw[:16000]}]) if len(raw)>1800 else raw
    await update.message.reply_text(out[:4000])

async def on_text(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    text=(update.message.text or update.message.caption or "").strip()
    if not text: return
    urls=extract_urls(text)
    web_snip=""
    if urls:
        try: web_snip=await fetch_urls(urls)
        except: web_snip=""
    elif need_web(text):
        try: web_snip=await search_and_fetch(text, hits=2)
        except: web_snip=""
    hist=await get_memory(uid)
    msgs=[{"role":"system","content":SYS}]
    if web_snip: msgs.append({"role":"system","content":"Актуальная сводка из интернета:\n"+web_snip})
    msgs+=hist+[{"role":"user","content":text}]
    reply=await asyncio.to_thread(ask_openai, msgs)
    hist.append({"role":"user","content":text})
    hist.append({"role":"assistant","content":reply})
    await save_memory(uid, hist[-MEM_LIMIT:])
    await update.message.reply_text(reply)

async def health(request): return web.Response(text="ok")

async def tg_webhook(request):
    try:
        data=await request.json()
        upd=Update.de_json(data, application.bot)
        await application.process_update(upd)
        return web.Response(text="ok")
    except Exception as e:
        return web.Response(text=str(e))

def build_app()->Application:
    app=ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("read", cmd_read))
    app.add_handler(CallbackQueryHandler(on_button, pattern="^start$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

async def main():
    global application
    await init_db()
    application=build_app()
    aio=web.Application()
    aio.add_routes([web.get("/health", health)])
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
