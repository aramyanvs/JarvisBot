import os
import httpx
from readability import Document
from lxml.html.clean import Cleaner
from lxml import html
from db import save_message

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
BOT_NAME = os.getenv("BOT_NAME", "Джарвис")

async def openai_chat(messages):
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    data = {"model": OPENAI_MODEL, "messages": messages, "temperature": 0.4}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post("https://api.openai.com/v1/chat/completions", json=data, headers=headers)
        r.raise_for_status()
        j = r.json()
        return j["choices"][0]["message"]["content"]

def build_system_prompt():
    return (
        f"Ты ассистент по имени {BOT_NAME}. Отвечай кратко, по делу, на русском, структурируй ответ."
        " Учитывай недавний контекст пользователя из истории."
    )

async def generate_reply(user_id: int, user_text: str, history: list[tuple[str, str]]):
    msgs = [{"role": "system", "content": build_system_prompt()}]
    for role, content in history[-10:]:
        msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": user_text})
    return await openai_chat(msgs)

async def web_fetch_and_summarize(url: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        doc = Document(r.text)
        html_clean = doc.summary(html_partial=True)
        tree = html.fromstring(html_clean)
        cleaner = Cleaner(scripts=True, javascript=True, style=True, links=False, forms=True, frames=True, comments=True, remove_unknown_tags=False)
        cleaned = cleaner.clean_html(tree)
        text = " ".join(cleaned.text_content().split())
        text = text[:6000]
    prompt = (
        "Кратко перескажи по пунктам содержимое страницы. Укажи 3-5 ключевых тезисов и практические выводы."
        f"\nСсылка: {url}\nТекст:\n{text}"
    )
    msgs = [{"role": "system", "content": build_system_prompt()}, {"role": "user", "content": prompt}]
    return await openai_chat(msgs)
