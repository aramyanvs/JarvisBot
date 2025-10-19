import asyncio
import importlib
import logging

logging.basicConfig(level=logging.INFO)

async def main():
    try:
        patch_web = importlib.import_module("weblayer")
        logging.info("✅ WebLayer loaded successfully")
    except ModuleNotFoundError:
        logging.warning("⚠️ WebLayer not found — continuing without it")

    try:
        main_app = importlib.import_module("main")
        logging.info("🚀 Main app module imported")
        if hasattr(main_app, "run"):
            await asyncio.to_thread(main_app.run)
        elif hasattr(main_app, "main"):
            await main_app.main()
        else:
            logging.error("❌ No run() or main() function found in main.py")
    except Exception as e:
        logging.exception(f"❌ Failed to start main module: {e}")

if __name__ == "__main__":
    asyncio.run(main())
