import os
import asyncio
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from handlers import start, help_command, handle_message
from db import init_db, close_db

async def main():
    await init_db()

    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    webhook_url = os.getenv("WEBHOOK_URL")
    port = int(os.getenv("PORT", "8080"))

    if webhook_url:
        await app.bot.set_webhook(url=webhook_url)
        await app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=os.environ["TELEGRAM_BOT_TOKEN"],
        )
    else:
        await app.run_polling()

    await close_db()

if __name__ == "__main__":
    asyncio.run(main())

