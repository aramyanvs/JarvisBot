import os, asyncio
import weblayer
import main
from aiohttp import web

PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

async def tg_webhook(request):
    data = await request.json()
    upd = main.Update.de_json(data, main.application.bot)
    await main.application.process_update(upd)
    return web.Response(text="ok")

async def health(request):
    return web.Response(text="ok")

async def start_http():
    await main.init_db()
    main.application = main.build_app()
    await main.application.initialize()
    await main.application.start()
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/tgwebhook", tg_webhook)
    app.router.add_get("/migrate", main.migrate)
    if BASE_URL:
        await main.application.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)
    await main.set_menu(main.application)
    print("READY", flush=True)
    print("WEBHOOK:", f"{BASE_URL}/tgwebhook", flush=True)
    return app

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = loop.run_until_complete(start_http())
    web.run_app(app, host="0.0.0.0", port=PORT)
