from telegram import Update
from telegram.ext import ContextTypes

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я готов к работе. Напиши свой запрос.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Просто отправь сообщение, и я отвечу на него.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from main import generate_reply
    text = (update.message.text or "").strip()
    if not text:
        return
    await update.message.chat.send_action("typing")
    answer = await generate_reply(text, user_id=update.effective_user.id)
    if not answer:
        answer = "Не понял, попробуй переформулировать."
    await update.message.reply_text(answer)
