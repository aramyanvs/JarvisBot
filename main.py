import os, asyncio, structlog
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from handlers import start, help_command, reset_command, stats_command, mode_command, web_command, shutdown_command, ping_command, handle_message
from db import init_db, close_db

logger = structlog.get_logger()
TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", "8080"))
URL_PATH = "tgwebhook"

async def on_startup(app: Application):
    await init_db()
    logger.info("startup_ok")

async def on_shutdown(app: Application):
    await close_db()
    logger.info("shutdown_ok")

def build_app():
    app = Application.builder().token(TOKEN).post_init(on_startup).post_stop(on_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("mode", mode_command))
    app.add_handler(CommandHandler("web", web_command))
    app.add_handler(CommandHandler("shutdown", shutdown_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    return app

def main():
    app = build_app()
    if WEBHOOK_URL:
        app.run_webhook(webhook_url=f"{WEBHOOK_URL}", listen="0.0.0.0", port=PORT, url_path=URL_PATH)
    else:
        app.run_polling()

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN required")
    main()
