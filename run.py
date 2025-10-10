import os
import asyncio
import weblayer  # включает интернет-слой до импорта main
import main
from aiohttp import web

async def _start():
    if hasattr(main, "start_http"):
        return await main.start_http()
    if hasattr(main, "main"):
        await main.main()
        return None
    raise RuntimeError("main.py must expose start_http() or main()")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = loop.run_until_complete(_start())
    if isinstance(app, web.Application):
        port = int(os.getenv("PORT", "10000"))
        web.run_app(app, host="0.0.0.0", port=port)
    else:
        loop.run_forever()
