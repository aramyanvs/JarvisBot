import os, time, structlog
from telegram import Update
from telegram.ext import ContextTypes
from core import generate_reply, web_smart_summary
from db import ensure_user, save_message, get_history, reset_history, get_stats, set_mode, get_mode

logger = structlog.get_logger()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
BOT_NAME = os.getenv("BOT_NAME", "Джарвис")
_last_msg_at = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update.effective_user)
    await update.message.reply_text(f"Я {BOT_NAME}. Готов к работе.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/mode [short|long]\n/reset\n/stats\n/web <запрос>\n/ping\n/shutdown")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reset_history(update.effective_user.id)
    await update.message.reply_text("Контекст очищен.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cnt, chars = await get_stats(update.effective_user.id)
    await update.message.reply_text(f"Сообщений: {cnt}\nСимволов в истории: {chars}")

async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0] in ("short","long"):
        await set_mode(update.effective_user.id, context.args[0])
        await update.message.reply_text(f"Режим: {context.args[0]}")
    else:
        m = await get_mode(update.effective_user.id)
        await update.message.reply_text(f"Текущий режим: {m}")

async def web_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args).strip()
    if not q:
        await update.message.reply_text("Нужен запрос после /web")
        return
    await ensure_user(update.effective_user)
    await save_message(update.effective_user.id, "user", f"/web {q}")
    summary = await web_smart_summary(q)
    await save_message(update.effective_user.id, "assistant", summary)
    await update.message.reply_text(summary, disable_web_page_preview=True)

async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def shutdown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text("Недостаточно прав.")
        return
    await update.message.reply_text("Отключаюсь.")
    await context.application.stop()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    now = time.time()
    if uid in _last_msg_at and now - _last_msg_at[uid] < 1.5:
        return
    _last_msg_at[uid] = now
    await ensure_user(update.effective_user)
    text = update.message.text.strip()
    await save_message(uid, "user", text)
    hist = await get_history(uid, limit=30)
    mode = await get_mode(uid)
    reply = await generate_reply(uid, text, hist, mode)
    await save_message(uid, "assistant", reply)
    await update.message.reply_text(reply, disable_web_page_preview=True)
