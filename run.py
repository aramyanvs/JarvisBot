import os, asyncio
from aiohttp import web
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import main

PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

async def start_http():
    await main.init_db()

    app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
    if hasattr(main, "cmd_start"):
        app_bot.add_handler(CommandHandler("start", main.cmd_start))
    if hasattr(main, "cmd_ping"):
        app_bot.add_handler(CommandHandler("ping", main.cmd_ping))
    if hasattr(main, "cmd_reset"):
        app_bot.add_handler(CommandHandler("reset", main.cmd_reset))
    if hasattr(main, "cmd_read"):
        app_bot.add_handler(CommandHandler("read", main.cmd_read))
    if hasattr(main, "cmd_say"):
        app_bot.add_handler(CommandHandler("say", main.cmd_say))
    if hasattr(main, "on_button"):
        app_bot.add_handler(CallbackQueryHandler(main.on_button, pattern="^start$"))
    if hasattr(main, "on_voice"):
        app_bot.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, main.on_voice))
    if hasattr(main, "on_text"):
        app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main.on_text))

    await app_bot.initialize()
    await app_bot.start()

    main.application = app_bot

    app = web.Application()
    app.router.add_get("/health", main.health)
    app.router.add_post("/tgwebhook", main.tg_webhook)
    if hasattr(main, "migrate"):
        app.router.add_get("/migrate", main.migrate)

    if BASE_URL:
        await app_bot.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)

    if hasattr(main, "set_menu"):
        await main.set_menu(app_bot)

    print("READY", flush=True)
    if BASE_URL:
        print("WEBHOOK:", f"{BASE_URL}/tgwebhook", flush=True)
    return app

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    aio_app = loop.run_until_complete(start_http())
    web.run_app(aio_app, host="0.0.0.0", port=PORT)
