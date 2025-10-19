import os, asyncio, json, re
import asyncpg
from aiohttp import web
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from openai import AsyncOpenAI

PORT=int(os.getenv("PORT", "10000"))
BASE_URL=os.getenv("BASE_URL","").rstrip("/")
DB_URL=os.getenv("DB_URL","")
TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
OPENAI_API_KEY=os.getenv("OPENAI_API_KEY","")
OPENAI_MODEL=os.getenv("OPENAI_MODEL","gpt-4o-mini")
ALWAYS_WEB=os.getenv("ALWAYS_WEB","true").lower()=="true"
MEMORY_LIMIT=int(os.getenv("MEMORY_LIMIT","1500"))
DEFAULT_LANG=os.getenv("LANGUAGE","ru")
VOICE_MODE=os.getenv("VOICE_MODE","false").lower()=="true"

application: Application|None=None
oc=AsyncOpenAI(api_key=OPENAI_API_KEY)

async def db_conn():
    return await asyncpg.connect(DB_URL)

async def init_db():
    c=await db_conn()
    try:
        await c.execute("""
        create table if not exists users(
            user_id bigint primary key,
            mode text default 'chat',
            voice boolean default false,
            lang text default 'ru',
            translate_to text default 'en',
            created_at timestamptz default now()
        )""")
        await c.execute("""
        create table if not exists memory(
            id bigserial primary key,
            user_id bigint references users(user_id) on delete cascade,
            role text,
            content text,
            ts timestamptz default now()
        )""")
        await c.execute("create index if not exists idx_memory_user_ts on memory(user_id, ts)")
    finally:
        await c.close()

async def get_user(uid:int):
    c=await db_conn()
    try:
        row=await c.fetchrow("select user_id,mode,voice,lang,translate_to from users where user_id=$1",uid)
        if not row:
            await c.execute("insert into users(user_id,mode,voice,lang,translate_to) values($1,'chat',$2,$3,'en') on conflict do nothing",
                            uid, VOICE_MODE, DEFAULT_LANG)
            row=await c.fetchrow("select user_id,mode,voice,lang,translate_to from users where user_id=$1",uid)
        return dict(row)
    finally:
        await c.close()

async def save_message(uid:int, role:str, content:str):
    c=await db_conn()
    try:
        await c.execute("insert into memory(user_id,role,content) values($1,$2,$3)", uid, role, content)
        ids=await c.fetch("select id from memory where user_id=$1 order by ts desc offset $2", uid, MEMORY_LIMIT)
        if ids:
            max_keep=min(ids)[0]
            await c.execute("delete from memory where user_id=$1 and id<$2", uid, max_keep)
    finally:
        await c.close()

async def load_history(uid:int):
    c=await db_conn()
    try:
        rows=await c.fetch("select role,content from memory where user_id=$1 order by ts asc limit $2", uid, MEMORY_LIMIT)
        return [{"role":r["role"],"content":r["content"]} for r in rows]
    finally:
        await c.close()

async def summarize_url(text:str):
    try:
        import httpx, bs4
        urls=re.findall(r'https?://\S+', text)
        if not urls: return None
        url=urls[0]
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r=await client.get(url)
            html=r.text
        soup=bs4.BeautifulSoup(html,"lxml")
        title=(soup.title.text.strip() if soup.title else url)[:200]
        for tag in soup(["script","style","noscript"]): tag.decompose()
        content=" ".join(soup.get_text(" ").split())[:6000]
        if not content: return None
        prompt=f"Коротко и по делу перескажи содержимое страницы «{title}» для пользователя. Дай 5–8 пунктов и вывод."
        msg=[{"role":"system","content":"Ты помощник, который делает краткие сводки."},
             {"role":"user","content":prompt+"\n\nТекст:\n"+content}]
        res=await oc.chat.completions.create(model=OPENAI_MODEL,messages=msg,temperature=0.2)
        return f"Сводка по ссылке: {url}\n\n{res.choices[0].message.content}"
    except Exception:
        return None

async def ai_reply(uid:int, text:str):
    hist=await load_history(uid)
    sys="Ты полезный ассистент. Отвечай кратко и по делу."
    msgs=[{"role":"system","content":sys}]+hist+[{"role":"user","content":text}]
    if ALWAYS_WEB:
        s=await summarize_url(text)
        if s:
            msgs.append({"role":"system","content":"Свежая веб-выжимка:\n"+s})
    res=await oc.chat.completions.create(model=OPENAI_MODEL,messages=msgs,temperature=0.3)
    return res.choices[0].message.content

async def cmd_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    await get_user(uid)
    await update.message.reply_text("Привет! Я онлайн. Пиши сообщение или пришли ссылку — сделаю краткую сводку.")

async def on_text(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    uid=update.effective_user.id
    text=update.message.text.strip()
    await get_user(uid)
    await save_message(uid,"user",text)
    reply=await ai_reply(uid,text)
    await save_message(uid,"assistant",reply)
    await update.message.reply_text(reply)

async def tg_webhook(request):
    data=await request.json()
    upd=Update.de_json(data, application.bot)
    await application.process_update(upd)
    return web.Response(text="ok")

async def health(request):
    return web.Response(text="ok")

def build_app():
    return ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

async def start_http():
    global application
    await init_db()
    application=build_app()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    await application.initialize()
    await application.start()
    app=web.Application()
    app.add_routes([web.get("/health", health)])
    app.add_routes([web.post("/tgwebhook", tg_webhook)])
    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)
    return app

async def main():
    app=await start_http()
    runner=web.AppRunner(app)
    await runner.setup()
    site=web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    await asyncio.Event().wait()

def run():
    loop=asyncio.get_event_loop()
    app=loop.run_until_complete(start_http())
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__=="__main__":
    run()
