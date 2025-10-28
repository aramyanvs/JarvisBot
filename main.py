import os
import asyncio
import logging
from openai import OpenAI
from telegram.ext import Application, CommandHandler, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bot")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", "8080"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL is not set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

client = OpenAI(api_key=OPENAI_API_KEY)

async def generate_reply(text: str, user_id: int | None = None) -> str:
    def _call():
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты полезный ассистент телеграм-бота. Отвечай кратко и по делу."},
                {"role": "user", "content": text}
            ],
            temperature=0.7,
        )
        return r.choices[0].message.content.strip()
    return await asyncio.to_thread(_call)

def main():
    from handlers import start, help_command, handle_message
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Starting webhook")
    app.run_webhook(webhook_url=WEBHOOK_URL, listen="0.0.0.0", port=PORT, url_path="tgwebhook")

if __name__ == "__main__":
    main()
