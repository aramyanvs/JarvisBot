import os, asyncio, structlog, httpx, re
from openai import OpenAI
from readability import Document
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS

logger = structlog.get_logger()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
BOT_NAME = os.getenv("BOT_NAME", "Джарвис")
client = OpenAI(api_key=OPENAI_API_KEY)

def _estimate_tokens(s: str) -> int:
    return max(1, len(s) // 4)

def _build_system(mode: str) -> str:
    tone = "кратко" if mode == "short" else "подробно"
    return f"Ты ассистент по имени {BOT_NAME}. Отвечай {tone}, по делу, на русском."

async def _openai_chat(messages):
    r = await asyncio.to_thread(lambda: client.chat.completions.create(model=MODEL, messages=messages, temperature=0.4))
    return r.choices[0].message.content.strip()

async def generate_reply(user_id: int, text: str, history: list[tuple[str,str]], mode: str) -> str:
    sys = {"role":"system","content":_build_system(mode)}
    msgs = [sys]
    total = _estimate_tokens(sys["content"])
    for role, content in history[-20:]:
        msgs.append({"role":role,"content":content})
        total += _estimate_tokens(content)
    msgs.append({"role":"user","content":text})
    total += _estimate_tokens(text)
    if total > 8000:
        keep = []
        acc = 0
        for role, content in reversed(history):
            keep.append((role, content))
            acc += _estimate_tokens(content)
            if acc > 4000:
                break
        summary = await _openai_chat([sys, {"role":"user","content":"Сожми контекст:\n"+ "\n".join(c for _,c in keep)}])
        msgs = [sys, {"role":"system","content":"Сжатый контекст: "+summary}, {"role":"user","content":text}]
    out = await _openai_chat(msgs)
    return out

async def web_smart_summary(query: str) -> str:
    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, safesearch="moderate", region="ru-ru", max_results=5):
            if r and r.get("href"):
                results.append({"title":r.get("title",""),"url":r["href"],"snippet":r.get("body","")})
    texts = []
    async with httpx.AsyncClient(timeout=10) as client_http:
        for item in results[:4]:
            try:
                resp = await client_http.get(item["url"], follow_redirects=True, headers={"User-Agent":"Mozilla/5.0"})
                if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type",""):
                    continue
                doc = Document(resp.text)
                html = doc.summary()
                soup = BeautifulSoup(html, "lxml")
                for tag in soup(["script","style","noscript"]):
                    tag.decompose()
                text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
                if len(text) > 200:
                    texts.append((item["title"], item["url"], text[:4000]))
            except Exception:
                continue
    corpus = ""
    for t,u,txt in texts:
        corpus += f"Источник: {t}\n{u}\n{txt}\n\n"
    if not corpus:
        corpus = "Нет содержимого."
    prompt = [{"role":"system","content":"Сделай краткий, точный обзор. Добавь 3–5 пунктов и один вывод."},{"role":"user","content":f"Запрос: {query}\nМатериал:\n{corpus}"}]
    summary = await _openai_chat(prompt)
    return summary
