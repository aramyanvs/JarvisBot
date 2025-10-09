import os, asyncio, json, re
from aiohttp import web
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
import httpx
from telegram import Update

import main

PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

async def fetch_page(url, limit=7000, timeout=12):
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent":"Mozilla/5.0"}) as cl:
            r = await cl.get(url)
        html = r.text or ""
        soup = BeautifulSoup(html, "lxml")
        text = " ".join(s.strip() for s in soup.stripped_strings)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]
    except Exception as e:
        return f"[fetch error {url}: {e}]"

async def web_context(query, hits=2, per_page_chars=1200):
    try:
        results = []
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=hits, safesearch="moderate"):
                if r and r.get("href"):
                    results.append({"title": r.get("title",""), "url": r["href"], "snippet": r.get("body","")})
        chunks = []
        for it in results:
            body = await fetch_page(it["url"], limit=per_page_chars)
            t = (it["title"] or "No title").strip()
            sn = it["snippet"].strip() if it["snippet"] else ""
            chunks.append(f"• {t}\n{it['url']}\n{sn}\n{body}")
        if not chunks:
            return ""
        return "Актуальные источники:\n\n" + "\n\n".join(chunks)
    except Exception as e:
        return f"[search error: {e}]"

def pick_text_container(data: dict):
    m = data.get("message") or data.get("edited_message") or data.get("channel_post") or data.get("edited_channel_post") or {}
    return m

async def tg_webhook(request):
    data = await request.json()
    msg = pick_text_container(data)
    text = msg.get("text") or msg.get("caption")
    if text:
        ctx = await web_context(text, hits=2, per_page_chars=1200)
        if ctx:
            injected = f"{text}\n\n[WEB]\n{ctx[:3500]}"
            if "text" in msg:
                msg["text"] = injected
            elif "caption" in msg:
                msg["caption"] = injected
    upd = Update.de_json(data, main.application.bot)
    await main.application.process_update(upd)
    return web.Response(text="ok")

async def health(_):
    return web.Response(text="ok")

async def start_http():
    await main.init_db()
    app = main.build_app()
    main.application = app
    await app.initialize()
    await app.start()
    aio = web.Application()
    aio.router.add_get("/health", health)
    aio.router.add_post("/tgwebhook", tg_webhook)
    if BASE_URL:
        await app.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)
    print("READY", flush=True)
    print("WEBHOOK:", f"{BASE_URL}/tgwebhook" if BASE_URL else "(not set)", flush=True)
    runner = web.AppRunner(aio)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(start_http())
