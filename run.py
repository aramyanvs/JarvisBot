import os, asyncio
from aiohttp import web
import main
import patch_web

async def start_http():
    await main.init_db()
    global_app = main.build_app()
    main.application = global_app
    await main.application.initialize()
    await main.application.start()

    app = web.Application()
    app.router.add_get("/health", main.health)
    app.router.add_post("/tgwebhook", main.tg_webhook)
    app.router.add_get("/migrate", main.migrate)

    base_url = os.getenv("PUBLIC_URL", "").rstrip("/")
    if base_url:
        await main.application.bot.set_webhook(f"{base_url}/tgwebhook", drop_pending_updates=True)

    await main.set_menu(main.application)
    print("READY", flush=True)
    print("WEBHOOK:", f"{base_url}/tgwebhook", flush=True)
    return app

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    aio_app = loop.run_until_complete(start_http())
    port = int(os.getenv("PORT", "10000"))
    web.run_app(aio_app, host="0.0.0.0", port=port)
