import re, httpx
from bs4 import BeautifulSoup
from readability import Document
from duckduckgo_search import DDGS
import main

UA = "Mozilla/5.0"
MAX_SNIPPET = 12000

def _extract_urls(q: str):
    return re.findall(r"https?://\S+", q or "")

def _fetch_url(u: str, limit: int = 4000) -> str:
    try:
        with httpx.Client(follow_redirects=True, headers={"User-Agent": UA}, timeout=20) as cl:
            r = cl.get(u)
        ct = (r.headers.get("content-type") or "").lower()
        if "text/html" in ct or "<html" in r.text[:500].lower():
            html = Document(r.text).summary()
            soup = BeautifulSoup(html, "lxml")
            text = soup.get_text("\n", strip=True)
        else:
            text = r.text
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:limit]
    except:
        return ""

def _fetch_urls(urls, limit_chars=12000) -> str:
    out = []
    for u in urls[:3]:
        t = _fetch_url(u, limit=4000)
        if t:
            out.append(t)
        if sum(len(x) for x in out) >= limit_chars:
            break
    return "\n\n".join(out)[:limit_chars]

def _search_and_fetch(query: str, hits: int = 2, limit_chars: int = 12000) -> str:
    links = []
    try:
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=hits, safesearch="moderate"):
                if r and r.get("href"):
                    links.append(r["href"])
    except:
        pass
    return _fetch_urls(links, limit_chars) if links else ""

def _last_user_text(messages):
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, str):
                return c.strip()
    return ""

_orig_ask = getattr(main, "ask_openai")

def ask_openai_with_web(messages, temperature=0.3, max_tokens=800):
    q = _last_user_text(messages)
    web_snip = ""
    urls = _extract_urls(q)
    if urls:
        web_snip = _fetch_urls(urls)
    else:
        if q:
            web_snip = _search_and_fetch(q, hits=2, limit_chars=MAX_SNIPPET)
    if web_snip:
        sys = {"role": "system", "content": "Актуальная сводка из интернета:\n" + web_snip}
        msgs = []
        inserted = False
        for m in messages:
            if not inserted and m.get("role") == "system":
                msgs.append(m)
                msgs.append(sys)
                inserted = True
            else:
                msgs.append(m)
        if not inserted:
            msgs = [sys] + list(messages)
        return _orig_ask(msgs, temperature=temperature, max_tokens=max_tokens)
    return _orig_ask(messages, temperature=temperature, max_tokens=max_tokens)

main.ask_openai = ask_openai_with_web
print("[WebLayer] Internet mode: ALWAYS ON", flush=True)
