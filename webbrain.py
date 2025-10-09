import os, asyncio, httpx
from bs4 import BeautifulSoup
from readability import Document
from duckduckgo_search import DDGS

UA = "Mozilla/5.0 (JarvisBot)"
ALWAYS_WEB = os.getenv("ALWAYS_WEB", "true").lower() == "true"

async def browse_web(query: str, hits: int = 3, max_len: int = 1800):
    try:
        links = []
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=hits):
                if r.get("href"):
                    links.append((r.get("title") or "", r["href"]))
        if not links:
            return ""
        results = []
        async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=15) as cl:
            for title, href in links:
                try:
                    r = await cl.get(href)
                    html = Document(r.text).summary()
                    text = BeautifulSoup(html, "lxml").get_text("\n", strip=True)
                    snippet = text[:max_len]
                    results.append(f"🔗 {title}\n{href}\n{snippet}")
                except:
                    continue
        return "\n\n".join(results[:hits])
    except Exception:
        return ""

async def inject_web_context(message: str) -> str:
    if not ALWAYS_WEB:
        return ""
    try:
        data = await asyncio.wait_for(browse_web(message), timeout=20)
        return data
    except Exception:
        return ""
