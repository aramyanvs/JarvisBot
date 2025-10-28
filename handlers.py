from telegram import Update
from telegram.ext import ContextTypes
from core import generate_reply, web_fetch_and_summarize
from db import save_user, save_message, fetch_context

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await save_user(user.id, user.username or "", user.full_name or "")
    await update.message.reply_text("Привет! Я Джарвис. Пиши запрос или команду /web <url>.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Поддерживаю диалог с памятью. Команда: /web <url> — прочитать и кратко ответить.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    await save_user(user.id, user.username or "", user.full_name or "")
    await save_message(user.id, role="user", content=text)
    history = await fetch_context(user.id, limit=12)
    reply = await generate_reply(user_id=user.id, user_text=text, history=history)
    await save_message(user.id, role="assistant", content=reply)
    await context.bot.send_message(chat_id=chat_id, text=reply)

async def web_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажи ссылку: /web https://...")
        return
    url = context.args[0].strip()
    user = update.effective_user
    await save_user(user.id, user.username or "", user.full_name or "")
    summary = await web_fetch_and_summarize(url)
    await save_message(user.id, role="assistant", content=summary)
    await update.message.reply_text(summary)
