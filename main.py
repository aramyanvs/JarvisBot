import os
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
from handlers import start, help_command, handle_message
from core import logger

TOKEN = os.environ["TELEGRAM_TOKEN"]
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", "8080"))

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if WEBHOOK_URL:
        app.run_webhook(webhook_url=WEBHOOK_URL, listen="0.0.0.0", port=PORT)
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
