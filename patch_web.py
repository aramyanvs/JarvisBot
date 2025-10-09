import asyncio, re, io, json, httpx
from bs4 import BeautifulSoup
from readability import Document
from duckduckgo_search import DDGS

import main

UA = "Mozilla/5.0"
MAX_SNIPPET = 12000
SEARCH_HITS = 2

async def fetch_url(url: str, limit: int = 4000) -> str:
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": UA}, timeout=25) as cl:
            r = await cl.get(url)
        ct = (r.headers.get("content-type") or "").lower()
        text = r.text
        if "text/html" in ct or "<html" in text[:500].lower():
            html = Document(text).summary()
            soup = BeautifulSoup(html, "lxml")
            text = soup.get_text("\n", strip=True)
        return re.sub(r"\n{3,}", "\n\n", text)[:limit]
    except Exception:
        return ""

def extract_urls(q: str):
    return re.findall(r"https?://\S+", q)

async def fetch_urls(urls, limit_chars=MAX_SNIPPET):
    out = []
    for u in urls[:3]:
        t = await fetch_url(u, limit=4000)
        if t:
            out.append(t)
    return ("\n\n".join(out))[:limit_chars]

async def search_and_fetch(query: str, hits: int = SEARCH_HITS, limit_chars: int = MAX_SNIPPET):
    links = []
    try:
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=hits, safesearch="moderate"):
                if r and r.get("href"):
                    links.append(r["href"])
    except Exception:
        pass
    return await fetch_urls(links, limit_chars) if links else ""

def need_web(q: str) -> bool:
    if "http://" in q or "https://" in q:
        return True
    t = q.lower()
    if re.search(r"\b20(2[4-9]|3\d)\b", t):
        return True
    keys = ["сейчас","сегодня","новост","курс","цена","сколько стоит","когда будет","последн","обнов","релиз","погода","расписан","матч","акции","доступно","вышел","итог"]
    return any(k in t for k in keys)

_orig_on_text = main.on_text
_orig_on_voice = main.on_voice

async def _wrapped_on_text(update, ctx):
    uid = update.effective_user.id
    text = (update.message.text or update.message.caption or "").strip()
    urls = extract_urls(text)
    web_snip = ""
    if urls:
        web_snip = await fetch_urls(urls)
    else:
        web_snip = await search_and_fetch(text, hits=2)

    if web_snip:
        sys_note = {"role": "system", "content": "Актуальная сводка из интернета:\n" + web_snip}
        try:
            hist = await main.get_memory(uid)
        except Exception:
            hist = []
        ctx.chat_data["__web_snip__"] = sys_note
    else:
        ctx.chat_data.pop("__web_snip__", None)

    return await _orig_on_text(update, ctx)

async def _wrapped_on_voice(update, ctx):
    return await _orig_on_voice(update, ctx)

main.on_text = _wrapped_on_text
main.on_voice = _wrapped_on_voice
