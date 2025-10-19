import os, asyncio, json, asyncpg, httpx
from aiohttp import web
from telegram import Update, BotCommand
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import AsyncOpenAI

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DB_URL = os.getenv("DB_URL") or os.getenv("DATABASE_URL", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", os.getenv("RAILWAY_TCP_PORT", "8000")))
LANGUAGE = os.getenv("LANGUAGE", "ru")
ALWAYS_WEB = os.getenv("ALWAYS_WEB", "true").lower() == "true"
MEMORY_LIMIT = int(os.getenv("MEMORY_LIMIT", "1500"))
VOICE_MODE = os.getenv("VOICE_MODE", "false").lower() == "true"

client = AsyncOpenAI(api_key=OPENAI_KEY)
application = None
_pool = None

async def db_connect():
    global _pool
    if _pool is None and DB_URL:
        _pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=3)
    return _pool

async def init_db():
    pool = await db_connect()
    if not pool:
        return
    async with pool.acquire() as c:
        await c.execute("create table if not exists users (id bigint primary key, lang text, persona text, created_at timestamptz default now())")
        await c.execute("create table if not exists memory (user_id bigint references users(id) on delete cascade, role text, content text, ts timestamptz default now())")
        await c.execute("create index if not exists memory_user_id_ts on memory(user_id, ts desc)")

async def add_memory(uid, role, text):
    pool = await db_connect()
    if pool:
        async with pool.acquire() as c:
            await c.execute("insert into memory(user_id, role, content) values($1,$2,$3)", uid, role, text)

async def get_memory(uid, limit=10):
    pool = await db_connect()
    if not pool:
        return []
    async with pool.acquire() as c:
        rows = await c.fetch("select role, content from memory where user_id=$1 order by ts desc limit $2", uid, limit)
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

async def ai_answer(user_id, text):
    history = await get_memory(user_id)
    msgs = [{"role": "system", "content": "Ты умный Telegram ассистент."}]
    msgs += history + [{"role": "user", "content": text}]
    try:
        r = await client.chat.completions.create(model=OPENAI_MODEL, messages=msgs)
        ans = r.choices[0].message.content.strip()
    except Exception as e:
        ans = f"Ошибка AI: {e}"
    await add_memory(user_id, "user", text)
    await add_memory(user_id, "assistant", ans)
    return ans

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pool = await db_connect()
    if pool:
        async with pool.acquire() as c:
            await c.execute("insert into users(id, lang) values($1,$2) on conflict do nothing", uid, LANGUAGE)
    await update.message.reply_text("Привет! Я готов. Напиши сообщение.")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я бот с AI и доступом в интернет. Просто напиши вопрос.")

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid = update.effective_user.id
    if text.startswith("http"):
        try:
            async with httpx.AsyncClient(timeout=10) as h:
                r = await h.get(text)
            content = r.text[:4000]
            answer = await ai_answer(uid, f"Проанализируй сайт:\n\n{content}")
        except Exception:
            answer = "Не удалось открыть ссылку."
    else:
        answer = await ai_answer(uid, text)
    await update.message.reply_text(answer)

async def on_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("Не понял. Напиши текст.")

async def health(request):
    return web.Response(text="ok")

async def tg_webhook(request):
    global application
    if not application:
        return web.Response(status=503, text="not ready")
    data = await request.text()
    try:
        payload = json.loads(data)
    except:
        return web.Response(status=400, text="bad json")
    upd = Update.de_json(payload, application.bot)
    await application.process_update(upd)
    return web.Response(text="ok")

def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.ALL, on_unknown))
    return app

async def start_http():
    global application
    await init_db()
    application = build_app()
    await application.initialize()
    await application.start()
    aio = web.Application()
    aio.add_routes([web.get("/health", health)])
    aio.add_routes([web.post("/tgwebhook", tg_webhook)])
    runner = web.AppRunner(aio)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)
    print("BOT READY", flush=True)
    print(f"Webhook: {BASE_URL}/tgwebhook", flush=True)

async def main():
    await start_http()
    await asyncio.Event().wait()

def run():
    asyncio.run(main())

if __name__ == "__main__":
    run()
