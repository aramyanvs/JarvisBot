import os
import asyncio
from aiohttp import web
import main as appmod

try:
    import patch_web
    print("patch_web loaded", flush=True)
except Exception as e:
    print(f"patch_web skipped: {e}", flush=True)

async def bootstrap():
    if hasattr(appmod, "start_http"):
        return await appmod.start_http()
    if hasattr(appmod, "init_db"):
        await appmod.init_db()
    app = web.Application()
    if hasattr(appmod, "health"):
        app.router.add_get("/health", appmod.health)
    if hasattr(appmod, "tg_webhook"):
        app.router.add_post("/tgwebhook", appmod.tg_webhook)
    if hasattr(appmod, "migrate"):
        app.router.add_get("/migrate", appmod.migrate)
    return app

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    loop = asyncio.get_event_loop()
    aio_app = loop.run_until_complete(bootstrap())
    print("READY", flush=True)
    print(f"Listening on 0.0.0.0:{port}", flush=True)
    web.run_app(aio_app, host="0.0.0.0", port=port)
