import os
import asyncio
from urllib.parse import urlparse
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
from handlers import start, help_command, handle_message, web_command
from db import init_db

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
PORT = int(os.getenv("PORT", "10000"))
p = urlparse(WEBHOOK_URL)
URL_PATH = p.path.strip("/")

async def on_startup(app):
    await init_db()

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("web", web_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_webhook(webhook_url=f"{WEBHOOK_URL}", listen="0.0.0.0", port=PORT, url_path=URL_PATH)

if __name__ == "__main__":
    main()
