import re
import uuid
import tempfile
from telegram import Update
from telegram.ext import CommandHandler, MessageHandler, filters, ContextTypes
from config import ALWAYS_WEB, VOICE_MODE
from db import get_user, set_user, get_memory, add_memory, reset_memory
from webutils import web_context, weather, currency
from parse_utils import parse_file
from llm import sys_prompt, empathize, llm, to_tts, transcribe, translate_text, summarize_text, openai_image

def add_handlers(app):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("setlang", cmd_setlang))
    app.add_handler(CommandHandler("personality", cmd_personality))
    app.add_handler(CommandHandler("voicetrans", cmd_voicetrans))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("currency", cmd_currency))
    app.add_handler(CommandHandler("image", cmd_image))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await get_user(uid)
    txt = "Привет! Я Jarvis. Доступно: /weather <город>, /currency <база> [символы], /reset, /setlang <ru|en|...>, /personality <assistant|professor|sarcastic>, /voicetrans <on|off>, /image <промпт>. Пиши или пришли голос."
    await update.message.reply_text(txt)

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await reset_memory(uid)
    await update.message.reply_text("Окей, контекст очищен.")

async def cmd_setlang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Пример: /setlang ru")
        return
    lang = context.args[0].lower()
    await set_user(uid, lang=lang)
    await update.message.reply_text(f"Язык по умолчанию: {lang}")

async def cmd_personality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Варианты: assistant, professor, sarcastic")
        return
    p = context.args[0].lower()
    if p not in ["assistant", "professor", "sarcastic"]:
        await update.message.reply_text("Неверно. Варианты: assistant, professor, sarcastic")
        return
    await set_user(uid, persona=p)
    await update.message.reply_text(f"Персональность: {p}")

async def cmd_voicetrans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Использование: /voicetrans on|off")
        return
    on = context.args[0].lower() in ["on", "1", "true", "yes"]
    await set_user(uid, voicetrans=on)
    await update.message.reply_text("Перевод voice: " + ("включён" if on else "выключен"))

async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /weather Moscow")
        return
    city = " ".join(context.args)
    try:
        w = await weather(city)
        await update.message.reply_text(w)
    except Exception:
        await update.message.reply_text("Не удалось получить погоду.")

async def cmd_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /currency usd rub,eur")
        return
    base = context.args[0]
    syms = context.args[1] if len(context.args) > 1 else "RUB,EUR"
    try:
        r = await currency(base, syms.upper())
        await update.message.reply_text(r)
    except Exception:
        await update.message.reply_text("Не удалось получить курсы.")

async def cmd_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Пример: /image astronaut cat in neon city")
        return
    prompt = " ".join(context.args)
    try:
        img = await openai_image(prompt)
        fn = f"image_{uuid.uuid4().hex}.png"
        await update.message.reply_photo(photo=img, filename=fn)
    except Exception:
        await update.message.reply_text("Не удалось сгенерировать изображение.")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    from db import db_conn
    c = await db_conn()
    rows = await c.fetch("select sum(length(content)) from memory where user_id=$1", uid)
    await c.close()
    used = rows[0]["sum"] or 0
    await update.message.reply_text(f"📊 Вы использовали ~{used} символов памяти.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from config import LANG, ALWAYS_WEB
    uid = update.effective_user.id
    u = await get_user(uid)
    text = update.message.text or ""
    lang = u["lang"] or ("ru" if re.search(r"[А-Яа-яЁё]", text) else "en")
    mood = await empathize(text, lang)
    hist = await get_memory(uid)
    webtxt = ""
    if ALWAYS_WEB or re.search(r"https?://|новост|news|ссылк|прочитай|итог|resume|summar", text, re.I):
        webtxt = await web_context(text)
        if webtxt:
            hist.append({"role": "system", "content": "Веб-контент:\n" + webtxt})
    sys = sys_prompt(u["persona"], lang)
    hist2 = hist + [{"role": "user", "content": text}]
    try:
        reply = await llm(hist2, sys)
    except Exception:
        reply = "Проблема с моделью."
    await add_memory(uid, "user", text)
    await add_memory(uid, "assistant", reply)
    await update.message.reply_text(mood + "\n\n" + reply)

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    doc = update.message.document
    f = await doc.get_file()
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        await f.download_to_drive(tmp.name)
        content = await parse_file(tmp.name, doc.file_name or "file")
    lang = u["lang"]
    s = await summarize_text(content[:18000], lang)
    await add_memory(uid, "user", "[файл загружен]")
    await add_memory(uid, "assistant", s)
    await update.message.reply_text(s)

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    v = update.message.voice or update.message.audio
    if not v:
        await update.message.reply_text("Голос не найден.")
        return
    f = await v.get_file()
    with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as tmp:
        await f.download_to_drive(tmp.name)
        try:
            text = await transcribe(tmp.name)
        except Exception:
            await update.message.reply_text("Не удалось распознать голос.")
            return
    lang = u["lang"] or ("ru" if re.search(r"[А-Яа-яЁё]", text) else "en")
    hist = await get_memory(uid)
    if u["voicetrans"] and u["translate_to"]:
        try:
            reply = await translate_text(text, u["translate_to"])
        except Exception:
            reply = "Не удалось перевести."
    else:
        webtxt = ""
        if ALWAYS_WEB or re.search(r"https?://|новост|news|ссылк|прочитай|итог|resume|summar", text, re.I):
            webtxt = await web_context(text)
            if webtxt:
                hist.append({"role": "system", "content": "Веб-контент:\n" + webtxt})
        sys = sys_prompt(u["persona"], lang)
        hist2 = hist + [{"role": "user", "content": text}]
        try:
            reply = await llm(hist2, sys)
        except Exception:
            reply = "Проблема с моделью."
    await add_memory(uid, "user", text)
    await add_memory(uid, "assistant", reply)
    if VOICE_MODE:
        try:
            audio = await to_tts(reply, "alloy")
            await update.message.reply_voice(voice=audio, caption=None)
        except Exception:
            await update.message.reply_text(reply)
    else:
        await update.message.reply_text(reply)
