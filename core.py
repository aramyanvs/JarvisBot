import os
import asyncio
import time
from typing import List, Dict, Any
import httpx
from readability import Document
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
from openai import OpenAI

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
client = OpenAI(api_key=OPENAI_API_KEY)

_user_rl: Dict[int, float] = {}
RL_SECONDS = 1.5

def _rate_limited(user_id: int) -> bool:
    now = time.time()
    last = _user_rl.get(user_id, 0.0)
    if now - last < RL_SECONDS:
        return True
    _user_rl[user_id] = now
    return False

async def ai_chat(messages: List[Dict[str, str]], model: str = "gpt-4o", temperature: float = 0.4) -> str:
    r = client.chat.completions.create(model=model, messages=messages, temperature=temperature)
    return (r.choices[0].message.content or "").strip()

async def generate_reply(user_id: int, text: str) -> str:
    if _rate_limited(user_id):
        return "Подожди чуть-чуть и напиши снова."
    if text.lower().startswith("/web ") or text.lower().startswith("!web "):
        q = text.split(" ", 1)[1].strip()
        return await answer_with_search(q)
    msgs = [
        {"role": "system", "content": "Ты полезный ассистент. Отвечай кратко и по делу."},
        {"role": "user", "content": text}
    ]
    return await ai_chat(msgs)

async def fetch_url(session: httpx.AsyncClient, url: str, timeout: float = 10.0) -> str:
    try:
        r = await session.get(url, timeout=timeout, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return ""
        html = r.text
        doc = Document(html)
        content_html = doc.summary()
        soup = BeautifulSoup(content_html, "lxml")
        text = soup.get_text("\n")
        return text[:4000]
    except Exception:
        return ""

async def search_ddg(query: str, max_results: int = 5) -> List[str]:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        urls = []
        for it in results:
            u = it.get("href") or it.get("url")
            if u and u.startswith("http"):
                urls.append(u)
        return urls[:max_results]
    except Exception:
        return []

async def answer_with_search(query: str) -> str:
    urls = await asyncio.to_thread(search_ddg, query, 5)
    if not urls:
        return "Не получилось найти источники."
    async with httpx.AsyncClient() as s:
        bodies = await asyncio.gather(*(fetch_url(s, u) for u in urls))
    context = ""
    picked = []
    for u, b in zip(urls, bodies):
        if b:
            picked.append(u)
            context += f"\n\nИсточник: {u}\n{b}"
        if len(context) > 10000:
            break
    if not picked:
        return "Источники недоступны."
    prompt = f"Вопрос: {query}\nИспользуй выдержки из источников ниже и ответь кратко и фактологично. В конце дай 2–3 ссылки.\n{context}\n\nОтвет:"
    msgs = [
        {"role": "system", "content": "Ты ассистент с доступом к найденным выдержкам. Отвечай по фактам из текста."},
        {"role": "user", "content": prompt}
    ]
    ans = await ai_chat(msgs, model="gpt-4o", temperature=0.2)
    tail = "\n\nСсылки:\n" + "\n".join(picked[:3])
    return ans + tail
