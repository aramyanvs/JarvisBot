import re
import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction
from openai import AsyncOpenAI
from webutils import web_search, fetch_url

OPENAI_API_KEY = ""
OPENAI_MODEL = "gpt-4o-mini"
ALWAYS_WEB = True

aclient = AsyncOpenAI(api_key=OPENAI_API_KEY)

async def process_text(text: str) -> str:
    if re.search(r"https?://|новост|news|resume|итог|summary|прочитай", text, re.I):
        try:
            search_results = await web_search(text, 3)
            webdata = []
            for item in search_results:
                url = item.get("url")
                if url:
                    try:
                        content = await fetch_url(url)
                        webdata.append(f"{item.get('title')}\n{url}\n{content[:1500]}")
                    except Exception:
                        continue
            webtext = "\n\n".join(webdata)[:5000]
        except Exception:
            webtext = ""
    else:
        webtext = ""

    try:
        msg = [{"role": "system", "content": "Отвечай кратко и по делу. Используй веб-контент, если он есть."}]
        if webtext:
            msg.append({"role": "system", "content": "Веб-контент:\n" + webtext})
        msg.append({"role": "user", "content": text})
        r = await aclient.chat.completions.create(model=OPENAI_MODEL, messages=msg, temperature=0.6, max_tokens=800)
        reply = r.choices[0].message.content.strip()
    except Exception:
        reply = "Проблема с моделью."
    return reply

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    text = update.message.text or ""
    reply = await process_text(text)
    await update.message.reply_text(reply)
