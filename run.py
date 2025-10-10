import os
os.environ.setdefault("WEB_ALWAYS", "1")

import asyncio
import inspect
from aiohttp import web
import weblayer  # noqa: F401  (устанавливает интернет-режим)
import main

PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

async def _manual_boot():
    app = web.Application()

    if hasattr(main, "health"):
        app.router.add_get("/health", main.health)
    else:
        async def _health(_): return web.Response(text="ok")
        app.router.add_get("/health", _health)

    if hasattr(main, "tg_webhook"):
        app.router.add_post("/tgwebhook", main.tg_webhook)

    if hasattr(main, "migrate"):
        app.router.add_get("/migrate", main.migrate)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    if BASE_URL and getattr(main, "application", None) and getattr(main.application, "bot", None):
        try:
            await main.application.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)
        except Exception:
            pass

    print("READY", flush=True)
    print("WEBHOOK:", f"{BASE_URL}/tgwebhook" if BASE_URL else "-", flush=True)
    await asyncio.Event().wait()

def _run_with_start_http():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = loop.run_until_complete(main.start_http())
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    if hasattr(main, "start_http") and inspect.iscoroutinefunction(main.start_http):
        _run_with_start_http()
    elif hasattr(main, "main") and inspect.iscoroutinefunction(main.main):
        asyncio.run(main.main())
    else:
        asyncio.run(_manual_boot())
