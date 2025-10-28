import os, io, asyncio, logging, tempfile
from typing import Dict, List, Any
from openai import OpenAI
from dotenv import load_dotenv
from telegram import Update, constants
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackContext, filters

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bot")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TG_SECRET_TOKEN = os.getenv("TG_SECRET_TOKEN")
URL_PATH = "tgwebhook"
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8080

if not TELEGRAM_TOKEN or not WEBHOOK_URL or not OPENAI_API_KEY:
    raise RuntimeError("ENV missing")

client = OpenAI(api_key=OPENAI_API_KEY)

SESSIONS: Dict[int, List[Dict[str, Any]]] = {}
MAX_HISTORY = 10

def session_messages(user_id: int) -> List[Dict[str, Any]]:
    return SESSIONS.setdefault(user_id, [{"role": "system", "content": "Ты краткий русскоязычный ассистент телеграм-бота. Отвечай по делу, без лишней болтовни."}])

def push_user(user_id: int, content: Any):
    msgs = session_messages(user_id)
    msgs.append({"role": "user", "content": content})
    if len(msgs) > MAX_HISTORY + 1:
        base = [msgs[0]]
        tail = msgs[-MAX_HISTORY:]
        SESSIONS[user_id] = base + tail

def push_assistant(user_id: int, text: str):
    msgs = session_messages(user_id)
    msgs.append({"role": "assistant", "content": text})
    if len(msgs) > MAX_HISTORY + 1:
        base = [msgs[0]]
        tail = msgs[-MAX_HISTORY:]
        SESSIONS[user_id] = base + tail

async def openai_chat(messages: List[Dict[str, Any]]) -> str:
    def _call():
        resp = client.chat.completions.create(model="gpt-4o-mini", messages=messages, temperature=0.6)
        return (resp.choices[0].message.content or "").strip()
    err = None
    for i in range(3):
        try:
            return await asyncio.to_thread(_call)
        except Exception as e:
            err = e
            await asyncio.sleep(0.5 * (i + 1))
    logger.exception("openai_chat failed: %s", err)
    return "Сервис занят, попробуйте ещё раз."

async def typing_action(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
        except Exception:
            pass

async def cmd_start(update: Update, context: CallbackContext):
    await update.effective_message.reply_text("Привет! Пришли текст, фото, документ или голос — отвечу.")

async def cmd_help(update: Update, context: CallbackContext):
    await update.effective_message.reply_text("/start — начать\n/help — помощь\n/ping — проверка\n/reset — сброс контекста")

async def cmd_ping(update: Update, context: CallbackContext):
    await update.effective_message.reply_text("pong")

async def cmd_reset(update: Update, context: CallbackContext):
    uid = update.effective_user.id if update.effective_user else None
    if uid and uid in SESSIONS:
        del SESSIONS[uid]
    await update.effective_message.reply_text("Контекст сброшен.")

async def handle_text(update: Update, context: CallbackContext):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    uid = update.effective_user.id if update.effective_user else 0
    await typing_action(update, context)
    push_user(uid, msg.text[:4000])
    reply = await openai_chat(session_messages(uid))
    push_assistant(uid, reply)
    await msg.reply_text(reply)

async def handle_photo(update: Update, context: CallbackContext):
    msg = update.effective_message
    if not msg or not msg.photo:
        return
    uid = update.effective_user.id if update.effective_user else 0
    await typing_action(update, context)
    photo = msg.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"
    user_text = msg.caption or "Опиши это изображение и ответь по делу."
    content = [
        {"type": "text", "text": user_text[:1000]},
        {"type": "image_url", "image_url": {"url": file_url}},
    ]
    push_user(uid, content)
    reply = await openai_chat(session_messages(uid))
    push_assistant(uid, reply)
    await msg.reply_text(reply)

async def handle_document(update: Update, context: CallbackContext):
    from pdfminer.high_level import extract_text as pdf_extract
    from docx import Document as Docx
    msg = update.effective_message
    if not msg or not msg.document:
        return
    uid = update.effective_user.id if update.effective_user else 0
    await typing_action(update, context)
    doc = msg.document
    f = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        await f.download_to_drive(tf.name)
        path = tf.name
    text = ""
    mime = doc.mime_type or ""
    name = (doc.file_name or "").lower()
    try:
        if mime == "application/pdf" or name.endswith(".pdf"):
            text = pdf_extract(path)[:8000]
        elif name.endswith(".docx"):
            d = Docx(path)
            text = "\n".join(p.text for p in d.paragraphs)[:8000]
        elif name.endswith(".txt"):
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()[:8000]
        else:
            text = f"Получен файл {doc.file_name or ''} ({mime}). Кратко опиши содержимое."
    except Exception:
        text = "Не удалось извлечь текст. Дай общий ответ по документу."
    user_prompt = (msg.caption or "").strip()
    merged = (user_prompt + "\n\n" if user_prompt else "") + text
    push_user(uid, merged if merged else "Проанализируй документ.")
    reply = await openai_chat(session_messages(uid))
    push_assistant(uid, reply)
    await msg.reply_text(reply)

async def handle_voice(update: Update, context: CallbackContext):
    msg = update.effective_message
    if not msg or not msg.voice:
        return
    uid = update.effective_user.id if update.effective_user else 0
    await typing_action(update, context)
    voice = msg.voice
    f = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg") as tf:
        await f.download_to_drive(tf.name)
        with open(tf.name, "rb") as rb:
            try:
                tr = client.audio.transcriptions.create(model="whisper-1", file=rb)
                text = tr.text.strip() if hasattr(tr, "text") else ""
            except Exception:
                text = ""
    if not text:
        await msg.reply_text("Не удалось распознать голос. Пришлите текст.")
        return
    push_user(uid, f"Пользователь сказал голосом: {text}")
    reply = await openai_chat(session_messages(uid))
    push_assistant(uid, reply)
    await msg.reply_text(reply)

async def on_error(update: object, context: CallbackContext):
    logger.exception("Unhandled error")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_error_handler(on_error)
    kwargs = {"webhook_url": WEBHOOK_URL, "listen": LISTEN_HOST, "port": LISTEN_PORT, "url_path": URL_PATH}
    if TG_SECRET_TOKEN:
        kwargs["secret_token"] = TG_SECRET_TOKEN
    logger.info("Starting webhook")
    app.run_webhook(**kwargs)

if __name__ == "__main__":
    main()
