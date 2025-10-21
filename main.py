import os
import asyncio
import logging
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)
from openai import OpenAI
from ddgs import DDGS
import asyncpg
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

client = OpenAI(api_key=OPENAI_API_KEY)

async def db_conn():
    return await asyncpg.connect(DATABASE_URL)

async def get_user(uid):
    conn = await db_conn()
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, persona TEXT DEFAULT 'assistant')"
    )
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)
    if not user:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1)", uid)
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)
    await conn.close()
    return user

async def generate_reply(text, user):
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": text}]
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        logger.error("Model error: %s", e)
        return "Ошибка с моделью. Попробуй позже."

def ddg_search(query: str):
    try:
        with DDGS(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as dd:
            return list(dd.text(query, max_results=5))
    except Exception as e:
        logger.warning("DDGS failed: %s", e)
        return []

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот активен.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        u = await get_user(uid)
        reply = await generate_reply(text, u)
        if reply:
            await update.message.reply_text(reply)
    except Exception as e:
        logger.exception("on_text failed: %s", e)
        await update.message.reply_text("Ошибка. Попробуй снова.")

async def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
