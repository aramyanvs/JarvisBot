from telegram import Update
from telegram.ext import ContextTypes
from core import logger, get_user, generate_reply

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я живой.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Напиши сообщение — отвечу тем же.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = await get_user(update.effective_user.id)
    reply = await generate_reply(update.message.text, user["id"])
    await update.message.reply_text(reply)
