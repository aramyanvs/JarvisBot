import asyncio
import signal
from aiohttp import web
from telegram import Update
from telegram.ext import Application
from config import TELEGRAM_BOT_TOKEN, BASE_URL, PORT, SENTRY_DSN
from handlers import add_handlers
from db import init_db
from llm import aclient, OPENAI_MODEL
import sentry_sdk

if SENTRY_DSN:
    sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=1.0)

application = None

async def tg_webhook(request):
    global application
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "err": "bad json"}, status=400)
    try:
        upd = Update.de_json(data, application.bot)
        asyncio.create_task(application.process_update(upd))
    except Exception:
        pass
    return web.json_response({"ok": True})

async def health(request):
    return web.Response(text="ok")

async def diag(request):
    try:
        r = await aclient.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"system","content":"ping"},{"role":"user","content":"ping"}],
            max_tokens=5,
            temperature=0
        )
        return web.json_response({"ok": True, "model": OPENAI_MODEL})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

def routes_app():
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/diag", diag)
    app.router.add_post("/tgwebhook", tg_webhook)
    return app

async def build_tg_app():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()
    add_handlers(app)
    return app

async def start_http():
    global application
    await init_db()
    application = await build_tg_app()
    await application.initialize()
    await application.start()
    aio = routes_app()
    runner = web.AppRunner(aio)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL.rstrip('/')}/tgwebhook", drop_pending_updates=True)
    return aio

async def main():
    await start_http()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop.set)
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    await stop.wait()
