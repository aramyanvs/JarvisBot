import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
BASE_URL = os.getenv("BASE_URL", "")
DB_URL = os.getenv("DB_URL", "")
ALWAYS_WEB = os.getenv("ALWAYS_WEB", "true").lower() == "true"
LANG = os.getenv("LANGUAGE", "ru")
MEM_LIMIT = int(os.getenv("MEMORY_LIMIT", "1500"))
VOICE_MODE = os.getenv("VOICE_MODE", "true").lower() == "true"
PORT = int(os.getenv("PORT", "8080"))
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
RATE_LIMIT = os.getenv("RATE_LIMIT", "30/minute")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "20"))
