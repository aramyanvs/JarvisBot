from telegram import Update
from telegram.ext import ContextTypes
from main import get_user, generate_reply, logger

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        u = await get_user(uid)
        reply = await generate_reply(text, u)
        if reply:
            await update.message.reply_text(reply)
    except Exception as e:
        logger.exception("on_text failed: %s", e)
        await update.message.reply_text("Ошибка. Попробуй снова.")
