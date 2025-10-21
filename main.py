import os
import asyncio
import logging
import structlog
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import sentry_sdk

load_dotenv()

SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.0)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = structlog.wrap_logger(logging.getLogger("bot"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip() or None

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL) if OPENAI_BASE_URL else OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = "Ты — лаконичный помощник. Отвечай по делу и без лишних фраз."

async def get_user(user_id: int):
    return {"id": user_id}

async def generate_reply(text: str, user: dict) -> str:
    if not text:
        return "Напиши текст сообщения."
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error("openai_error", error=str(e))
        return "Сервис модели недоступен. Попробуй позже."

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.effective_user.id
        txt = (update.message.text or "").strip()
        u = await get_user(uid)
        reply = await generate_reply(txt, u)
        if reply:
            await update.message.reply_text(reply)
    except Exception as e:
        logger.exception("handler_error: %s", e)
        await update.message.reply_text("Ошибка. Попробуй снова.")

def main():
    token = os.getenv("TELEGRAM_TOKEN", "")
    webhook_url = os.getenv("WEBHOOK_URL", "")
    port = int(os.getenv("PORT", "8080"))
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN is not set")
    if not webhook_url:
        raise RuntimeError("WEBHOOK_URL is not set")

    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path="",
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
