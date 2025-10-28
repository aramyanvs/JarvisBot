from telegram import Update
from telegram.ext import ContextTypes
from core import generate_reply

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Я на связи. Пиши запрос или используй /web <запрос> для поиска в интернете.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Команды:\n/start\n/help\n/web <запрос> — поиск и сводка по источникам.")

async def web_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = " ".join(context.args).strip()
    if not q:
        await update.message.reply_text("Напиши: /web твой запрос")
        return
    await update.message.chat.send_action(action="typing")
    ans = await generate_reply(update.effective_user.id, "/web " + q)
    await update.message.reply_text(ans)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    await update.message.chat.send_action(action="typing")
    ans = await generate_reply(update.effective_user.id, update.message.text.strip())
    await update.message.reply_text(ans)
