import re
import httpx
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from readability import Document
from lxml.html.clean import Cleaner
from duckduckgo_search import DDGS
from config import HTTP_TIMEOUT

ALLOWED_SCHEMES = {"http", "https"}
BLOCKED_NETLOCS = ("localhost", "127.", "0.0.0.", "10.", "192.168.", "172.")

def safe_url(url: str) -> bool:
    u = urlparse(url)
    if u.scheme not in ALLOWED_SCHEMES:
        return False
    h = u.hostname or ""
    return not any(h.startswith(p) for p in BLOCKED_NETLOCS)

async def ddg_search(q: str, k: int = 5):
    out = []
    with DDGS(timeout=10) as dd:
        for r in dd.text(q, max_results=k):
            out.append({"title": r.get("title", ""), "href": r.get("href", ""), "body": r.get("body", "")})
    return out

async def fetch_url(url: str) -> str:
    if not safe_url(url):
        return ""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True, headers={"User-Agent": "JarvisBot/1.0"}) as x:
        r = await x.get(url)
        html = r.text
    doc = Document(html)
    cleaned = doc.summary()
    cleaner = Cleaner(style=True, scripts=True, comments=True, links=False, meta=False, page_structure=False, processing_instructions=True, embedded=True, frames=True, forms=True, annoying_tags=True, remove_unknown_tags=False)
    cleaned = cleaner.clean_html(cleaned)
    soup = BeautifulSoup(cleaned, "html.parser")
    text = " ".join(soup.get_text(" ").split())
    return text[:12000]

async def web_context(query: str) -> str:
    try:
        results = await ddg_search(query, 5)
        chunks = []
        for r in results[:3]:
            u = r["href"]
            if not u or not u.startswith("http"):
                continue
            try:
                t = await fetch_url(u)
                chunks.append(f"{r['title']}\n{u}\n{t}\n")
            except Exception:
                continue
        return "\n\n".join(chunks)[:20000]
    except Exception:
        return ""

async def weather(city: str) -> str:
    u = f"https://wttr.in/{city}?format=j1"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as x:
        r = await x.get(u)
        j = r.json()
    cur = j["current_condition"][0]
    area = j["nearest_area"][0]["areaName"][0]["value"]
    temp = cur["temp_C"]
    feels = cur["FeelsLikeC"]
    w = cur["weatherDesc"][0]["value"]
    return f"{area}: {temp}°C (ощущается {feels}°C), {w}"

async def currency(base: str = "USD", symbols: str = "RUB,EUR") -> str:
    u = f"https://api.exchangerate.host/latest?base={base.upper()}&symbols={symbols.upper()}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as x:
        r = await x.get(u)
        j = r.json()
    rates = j.get("rates", {})
    items = [f"1 {base.upper()} = {rates[k]:.4f} {k}" for k in rates]
    return "\n".join(items) if items else "N/A"
