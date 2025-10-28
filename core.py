import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bot")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_APIKEY")

async def get_user(user_id: int):
    return {"id": user_id}

async def generate_reply(text: str, user_id: int) -> str:
    return text
