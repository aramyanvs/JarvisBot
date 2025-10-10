import os, re, asyncio, httpx
from bs4 import BeautifulSoup
from readability import Document
from duckduckgo_search import DDGS

UA = "Mozilla/5.0 (JarvisWebLayer)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
CONCURRENCY = 3
MAX_PER_SOURCE = 4000
MAX_BUNDLE = 12000


async def _fetch_html(client, url):
    try:
        r = await client.get(url, follow_redirects=True, headers={"User-Agent": UA})
        ct = (r.headers.get("content-type") or "").lower()
        text = r.text
        if "text/html" in ct or "<html" in text[:500].lower():
            html = Document(text).summary()
            soup = BeautifulSoup(html, "lxml")
            out = soup.get_text("\n", strip=True)
        else:
            out = text
        out = re.sub(r"\n{3,}", "\n\n", out)
        return out[:MAX_PER_SOURCE]
    except:
        return ""


async def fetch_url(url: str, limit=MAX_PER_SOURCE):
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        t = await _fetch_html(client, url)
        return t[:limit] if t else ""


async def fetch_urls(urls, limit_chars=MAX_BUNDLE):
    urls = [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))]
    if not urls:
        return ""
    out = []
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        async def run(u):
            async with sem:
                out.append(await _fetch_html(client, u))
        tasks = [asyncio.create_task(run(u)) for u in urls[:6]]
        await asyncio.gather(*tasks, return_exceptions=True)
    bundle = "\n\n".join([t for t in out if t])
    return bundle[:limit_chars]


async def search_and_fetch(query: str, hits: int = 4, limit_chars: int = MAX_BUNDLE):
    links = []
    try:
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=hits, safesearch="moderate"):
                if r and r.get("href"):
                    links.append(r["href"])
    except:
        links = []
    return await fetch_urls(links, limit_chars) if links else ""


def need_web(text: str) -> bool:
    """Возвращает True, если нужно использовать интернет"""
    always = os.getenv("ALWAYS_WEB", "false").lower() == "true"
    if always:
        return True
    t = (text or "").strip()
    if not t or t.startswith("/"):
        return False
    return True


def install():
    import main
    main.need_web = need_web
    main.fetch_url = fetch_url
    main.fetch_urls = fetch_urls
    main.search_and_fetch = search_and_fetch
    if os.getenv("ALWAYS_WEB", "false").lower() == "true":
        print("[WebLayer] Internet mode: ALWAYS ON")

install()
