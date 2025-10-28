import os
import logging
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from handlers import start, help_command, handle_message, web_command

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
URL_PATH = "tgwebhook"
PORT = int(os.environ.get("PORT", "8080"))

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("web", web_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_webhook(webhook_url=f"{WEBHOOK_URL}", listen="0.0.0.0", port=PORT, url_path=URL_PATH)

if __name__ == "__main__":
    main()
