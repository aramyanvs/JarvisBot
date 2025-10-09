import os, asyncio
from aiohttp import web
import main
import patch_web

async def start_http():
    await main.init_db()
    if hasattr(main, "build_app"):
        app_bot = main.build_app()
        main.application = app_bot
        await main.application.initialize()
        await main.application.start()
    elif hasattr(main, "application"):
        app_bot = main.application
    else:
        app_bot = None

    app = web.Application()
    app.router.add_get("/health", main.health)
    app.router.add_post("/tgwebhook", main.tg_webhook)
    if hasattr(main, "migrate"):
        app.router.add_get("/migrate", main.migrate)

    base_url = os.getenv("PUBLIC_URL", "").rstrip("/")
    if base_url and hasattr(main, "application"):
        await main.application.bot.set_webhook(f"{base_url}/tgwebhook", drop_pending_updates=True)

    if hasattr(main, "set_menu") and hasattr(main, "application"):
        await main.set_menu(main.application)

    print("READY", flush=True)
    if base_url:
        print("WEBHOOK:", f"{base_url}/tgwebhook", flush=True)
    return app

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    aio_app = loop.run_until_complete(start_http())
    port = int(os.getenv("PORT", "10000"))
    web.run_app(aio_app, host="0.0.0.0", port=port)
