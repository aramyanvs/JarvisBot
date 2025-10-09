import os, re, io, json, tempfile, asyncio
from typing import Any, Dict, List, Tuple

import asyncpg
import httpx
import pandas as pd
from bs4 import BeautifulSoup
from readability import Document
from duckduckgo_search import DDGS
from aiohttp import web
from openai import OpenAI

from pdfminer.high_level import extract_text as pdf_text
from docx import Document as Docx

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, BotCommand
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
DB_URL = os.getenv("DB_URL", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
MEM_LIMIT = int(os.getenv("MEMORY_LIMIT", "1500"))
LANG = os.getenv("LANGUAGE", "ru")
BASE_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

DEFAULT_LANG = (LANG or "ru").lower()
UA = "Mozilla/5.0"

client = OpenAI(api_key=OPENAI_KEY)

application: Application | None = None

def sys_prompt(lang: str) -> str:
    return (
        f"Ты Jarvis — умный, вежливый ассистент на {lang}. "
        f"Отвечай кратко и по делу, структурируй мысли, используй маркированные списки, если уместно. "
        f"Если в system приходит сводка из интернета, опирайся на неё и указывай, что это сводка. "
        f"Если пользователь явно просит перевод, отвечай только переводом без лишних комментариев."
    )

def detect_lang(s: str) -> str:
    t = s.strip()
    if not t:
        return DEFAULT_LANG
    cyr = re.search(r"[А-Яа-яЁё]", t)
    lat = re.search(r"[A-Za-z]", t)
    if cyr and not lat:
        return "ru"
    if lat and not cyr:
        return "en"
    if "¿" in t or "¡" in t or re.search(r"\b(hola|gracias)\b", t, re.I):
        return "es"
    if re.search(r"\b(merci|bonjour)\b", t, re.I):
        return "fr"
    return DEFAULT_LANG

def parse_translate_intent(s: str) -> str | None:
    m = re.search(r"(?:translate to|переведи на)\s+([a-zA-Zа-яА-ЯёЁ]+)", s, re.I)
    if not m:
        return None
    word = m.group(1).lower()
    m2 = {
        "russian": "ru", "русский": "ru", "русскийязык": "ru", "рус": "ru", "ру": "ru",
        "english": "en", "английский": "en", "англ": "en", "ен": "en",
        "spanish": "es", "испанский": "es",
        "french": "fr", "французский": "fr",
        "german": "de", "немецкий": "de",
        "italian": "it", "итальянский": "it",
        "portuguese": "pt", "португальский": "pt",
        "turkish": "tr", "турецкий": "tr",
        "arabic": "ar", "арабский": "ar",
        "chinese": "zh", "китайский": "zh",
        "japanese": "ja", "японский": "ja",
        "korean": "ko", "корейский": "ko",
        "hindi": "hi", "хинди": "hi",
        "ukrainian": "uk", "украинский": "uk",
        "kazakh": "kk", "казахский": "kk",
    }
    return m2.get(word, word[:2])

def mood_of(text: str) -> str:
    low = text.lower()
    if any(w in low for w in ["устал", "вымотал", "тяжело", "грусть", "печаль", "bad day", "tired"]):
        return "tired"
    if any(w in low for w in ["рад", "супер", "отлично", "классно", "great", "awesome"]):
        return "happy"
    if any(w in low for w in ["злюсь", "злой", "бесит", "angry"]):
        return "angry"
    return "neutral"

def empathize(text: str, mood: str, lang: str) -> str:
    if mood == "tired":
        return "Понимаю, это выматывает. Давай упростим задачу и сделаем первый шаг вместе 💪" if lang == "ru" else "I get it, that’s exhausting. Let’s simplify and take the first step together 💪"
    if mood == "happy":
        return "Круто! Поддерживаю темп — что дальше делаем? 🚀" if lang == "ru" else "Love it! Let’s keep the momentum — what’s next? 🚀"
    if mood == "angry":
        return "Слышу раздражение. Давай разберём причину по пунктам и быстро починим ⚙️" if lang == "ru" else "I hear the frustration. Let’s break down the cause and fix it fast ⚙️"
    return ""

async def db_conn():
    return await asyncpg.connect(DB_URL)

async def init_db():
    c = await db_conn()
    await c.execute("""
        create table if not exists users(
            user_id bigint primary key,
            memory jsonb default '[]'::jsonb,
            mode text default 'concise',
            voice boolean default true,
            lang text,
            translate_to text
        )
    """)
    await c.execute("alter table users alter column memory type jsonb using coalesce(memory,'[]'::jsonb)")
    await c.execute("alter table users alter column memory set default '[]'::jsonb")
    await c.execute("alter table users add column if not exists mode text default 'concise'")
    await c.execute("alter table users add column if not exists voice boolean default true")
    await c.execute("alter table users add column if not exists lang text")
    await c.execute("alter table users add column if not exists translate_to text")
    await c.close()

async def get_user(uid: int) -> Dict[str, Any]:
    c = await db_conn()
    r = await c.fetchrow("select * from users where user_id=$1", uid)
    await c.close()
    if not r:
        return {"user_id": uid, "memory": [], "mode": "concise", "voice": True, "lang": DEFAULT_LANG, "translate_to": DEFAULT_LANG}
    mem = r["memory"]
    if isinstance(mem, str):
        try:
            mem = json.loads(mem) if mem else []
        except:
            mem = []
    return {
        "user_id": r["user_id"],
        "memory": mem or [],
        "mode": r["mode"] or "concise",
        "voice": bool(r["voice"]),
        "lang": (r["lang"] or DEFAULT_LANG),
        "translate_to": (r["translate_to"] or DEFAULT_LANG),
    }

async def save_user(uid: int, memory: List[Dict[str, str]], mode: str, voice: bool, lang: str, translate_to: str):
    c = await db_conn()
    mem_str = json.dumps(memory, ensure_ascii=False)
    await c.execute(
        """
        insert into users(user_id, memory, mode, voice, lang, translate_to)
        values($1, $2::jsonb, $3, $4, $5, $6)
        on conflict(user_id) do update set
            memory = excluded.memory,
            mode = excluded.mode,
            voice = excluded.voice,
            lang = excluded.lang,
            translate_to = excluded.translate_to
        """,
        uid, mem_str, mode, voice, lang, translate_to
    )
    await c.close()

async def get_memory(uid: int) -> List[Dict[str, str]]:
    u = await get_user(uid)
    return u["memory"]

async def save_memory(uid: int, mem: List[Dict[str, str]]):
    u = await get_user(uid)
    await save_user(uid, mem, u["mode"], u["voice"], u["lang"], u["translate_to"])

def openai_chat(messages: List[Dict[str, str]], temperature: float = 0.3, max_tokens: int = 700) -> str:
    r = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return (r.choices[0].message.content or "").strip()

async def fetch_url(url: str, limit: int = 20000) -> str:
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": UA}, timeout=25) as cl:
        r = await cl.get(url)
    ct = (r.headers.get("content-type") or "").lower()
    text = ""
    if "text/html" in ct or "<html" in r.text[:500].lower():
        html = Document(r.text).summary()
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n", strip=True)
    elif "application/pdf" in ct or url.lower().endswith(".pdf"):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
            f.write(r.content)
            f.flush()
            try:
                text = pdf_text(f.name) or ""
            finally:
                try: os.remove(f.name)
                except: pass
    else:
        text = r.text
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:limit]

def read_txt(p: str) -> str:
    return open(p, "r", encoding="utf-8", errors="ignore").read()

def read_pdf(p: str) -> str:
    return pdf_text(p) or ""

def read_docx(p: str) -> str:
    d = Docx(p)
    return "\n".join([x.text for x in d.paragraphs])

def read_table(p: str) -> str:
    if p.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(p)
    else:
        df = pd.read_csv(p)
    buf = io.StringIO()
    df.head(80).to_string(buf)
    return buf.getvalue()

def read_any(p: str) -> str:
    pl = p.lower()
    if pl.endswith((".txt", ".md", ".log")):
        return read_txt(p)
    if pl.endswith(".pdf"):
        return read_pdf(p)
    if pl.endswith(".docx"):
        return read_docx(p)
    if pl.endswith((".csv", ".xlsx", ".xls")):
        return read_table(p)
    return read_txt(p)

def extract_urls(q: str) -> List[str]:
    return re.findall(r"https?://\S+", q)

async def fetch_urls(urls: List[str], limit_chars: int = 12000) -> str:
    out = []
    for u in urls[:3]:
        try:
            t = await fetch_url(u, limit=4000)
            if t:
                out.append(t)
        except:
            pass
    return "\n\n".join(out)[:limit_chars]

async def search_and_fetch(query: str, hits: int = 2, limit_chars: int = 12000) -> str:
    links: List[str] = []
    try:
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=hits, safesearch="moderate"):
                if r and r.get("href"):
                    links.append(r["href"])
    except:
        pass
    return await fetch_urls(links, limit_chars) if links else ""

def transcribe_file(path: str) -> str:
    with open(path, "rb") as f:
        r = client.audio.transcriptions.create(model="whisper-1", file=f)
    return (r.text or "").strip()

def tts_to_mp3(text: str, voice: str = "alloy") -> str:
    tmp = tempfile.mktemp(suffix=".mp3")
    with client.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts",
        voice=voice,
        input=text
    ) as resp:
        resp.stream_to_file(tmp)
    return tmp

def main_menu() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("☀️ Погода", callback_data="menu_weather"),
            InlineKeyboardButton("💸 Курс валют", callback_data="menu_currency"),
        ],
        [
            InlineKeyboardButton("🌍 Новости дня", callback_data="menu_news"),
            InlineKeyboardButton("🧠 Случайный факт", callback_data="menu_fact"),
        ],
        [
            InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings"),
        ],
    ]
    return InlineKeyboardMarkup(rows)

async def set_menu(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "запуск"),
        BotCommand("ping", "проверка"),
        BotCommand("reset", "сбросить память"),
        BotCommand("read", "прочитать URL/файл"),
        BotCommand("say", "озвучить текст"),
    ])

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await set_menu(ctx.application)
    await update.message.reply_text("Привет, я Jarvis 🤖", reply_markup=main_menu())

async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = await get_user(update.effective_user.id)
    await save_user(u["user_id"], [], u["mode"], u["voice"], u["lang"], u["translate_to"])
    await update.message.reply_text("Память очищена.")

async def cmd_read(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Формат: /read URL")
        return
    url = parts[1].strip()
    try:
        raw = await fetch_url(url)
    except Exception as e:
        await update.message.reply_text(f"Ошибка загрузки: {e}")
        return
    summary = raw
    if len(raw) > 1800:
        summary = openai_chat(
            [{"role": "system", "content": "Суммаризируй текст кратко и структурировано."},
             {"role": "user", "content": raw[:16000]}],
            temperature=0.2, max_tokens=600
        )
    await update.message.reply_text(summary[:4000])

async def cmd_say(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Формат: /say текст")
        return
    txt = parts[1].strip()
    mp3 = tts_to_mp3(txt, voice="alloy")
    try:
        with open(mp3, "rb") as f:
            await update.message.reply_audio(InputFile(f, filename="jarvis.mp3"))
    finally:
        try: os.remove(mp3)
        except: pass

async def on_menu_click(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "menu_weather":
        await q.edit_message_text("Введи: город или отправь локацию. Пример: `Погода Москва` или напиши /weather <город>", parse_mode="Markdown")
    elif data == "menu_currency":
        await q.edit_message_text("Напиши: `Курс usd` или `Курс eur`", parse_mode="Markdown")
    elif data == "menu_news":
        await q.edit_message_text("Напиши тему / ключевое слово — я принесу сводку.")
    elif data == "menu_fact":
        fact = openai_chat(
            [{"role": "system", "content": "Сгенерируй один интересный факт (1-2 предложения), без повторов и воды."},
             {"role": "user", "content": "Дай случайный факт."}],
            temperature=0.9, max_tokens=120
        )
        await q.edit_message_text(f"🧠 {fact}")
    elif data == "menu_settings":
        u = await get_user(update.effective_user.id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Язык интерфейса: {u['lang']}", callback_data="noop")],
            [InlineKeyboardButton(f"Озвучка: {'вкл' if u['voice'] else 'выкл'}", callback_data="toggle_voice")],
            [InlineKeyboardButton("Перевод: укажи целевой язык через текст: translate to <lang>", callback_data="noop")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_main")],
        ])
        await q.edit_message_text("Настройки", reply_markup=kb)
    elif data == "toggle_voice":
        u = await get_user(update.effective_user.id)
        await save_user(u["user_id"], u["memory"], u["mode"], not u["voice"], u["lang"], u["translate_to"])
        await q.edit_message_text(f"Озвучка теперь: {'вкл' if not u['voice'] else 'выкл'}")
    elif data == "back_main":
        await q.edit_message_text("Главное меню", reply_markup=main_menu())

def need_web(q: str) -> bool:
    t = q.lower()
    keys = ["сейчас", "сегодня", "новост", "курс", "цена", "сколько стоит", "когда будет", "последн", "обнов", "релиз", "погода", "расписан", "матч", "акции", "доступно", "вышел", "итог"]
    if any(k in t for k in keys): 
        return True
    if re.search(r"\b20(2[4-9]|3\d)\b", t): 
        return True
    if "http://" in t or "https://" in t: 
        return True
    return False

async def reply_text_logic(uid: int, text: str) -> Tuple[str, str]:
    u = await get_user(uid)
    user_lang = u["lang"]
    hist = u["memory"]

    tr_to = parse_translate_intent(text) or u["translate_to"]
    urls = extract_urls(text)
    web_snip = ""
    if urls:
        try: web_snip = await fetch_urls(urls)
        except: web_snip = ""
    elif need_web(text):
        try: web_snip = await search_and_fetch(text, hits=2)
        except: web_snip = ""

    msgs = [{"role": "system", "content": sys_prompt(user_lang)}]
    if web_snip:
        msgs.append({"role": "system", "content": "Актуальная сводка из интернета:\n" + web_snip})
    msgs += hist[-MEM_LIMIT:] + [{"role": "user", "content": text}]
    reply = ""
    try:
        if parse_translate_intent(text):
            src_lang = detect_lang(text)
            target_lang = tr_to or DEFAULT_LANG
            msgs = [
                {"role": "system", "content": f"Ты переводчик. Переведи следующий текст на язык: {target_lang}. Ответь только переводом."},
                {"role": "user", "content": re.sub(r"(?:translate to|переведи на)\s+[^\n]+", "", text, flags=re.I).strip()}
            ]
            reply = openai_chat(msgs, temperature=0.2, max_tokens=600)
            user_lang = target_lang
        else:
            mood = mood_of(text)
            em = empathize(text, mood, user_lang)
            if em:
                msgs.insert(1, {"role": "system", "content": f"Эмпатическая подсказка: {em}"})
            reply = openai_chat(msgs, temperature=0.4, max_tokens=700)
    except Exception as e:
        reply = f"⚠️ Ошибка ответа модели: {e}"

    hist.append({"role": "user", "content": text})
    hist.append({"role": "assistant", "content": reply})
    await save_user(uid, hist[-MEM_LIMIT:], u["mode"], u["voice"], user_lang, tr_to)
    return reply, user_lang

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or update.message.caption or "").strip()
    if not text:
        return
    reply, _lang = await reply_text_logic(uid, text)
    await update.message.reply_text(reply)

async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    v = update.message.voice or update.message.audio
    if not v:
        return
    f = await ctx.bot.get_file(v.file_id)
    p = await f.download_to_drive()
    loop = asyncio.get_event_loop()
    try:
        text = await loop.run_in_executor(None, transcribe_file, p)
    finally:
        try: os.remove(p)
        except: pass
    if not text:
        await update.message.reply_text("Не удалось распознать голос.")
        return

    reply, target_lang = await reply_text_logic(update.effective_user.id, text)

    u = await get_user(update.effective_user.id)
    if u["voice"]:
        try:
            mp3 = tts_to_mp3(reply, voice="alloy")
            try:
                with open(mp3, "rb") as f:
                    await update.message.reply_voice(InputFile(f, filename="jarvis.ogg"))
            finally:
                try: os.remove(mp3)
                except: pass
        except Exception as e:
            await update.message.reply_text(f"{reply}\n\n(Озвучка недоступна: {e})")
    else:
        await update.message.reply_text(reply)

async def health(request):
    return web.Response(text="ok")

async def tg_webhook(request):
    try:
        data = await request.json()
        upd = Update.de_json(data, application.bot)
        await application.process_update(upd)
        return web.Response(text="ok")
    except Exception as e:
        return web.Response(status=200, text=str(e))

def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("read", cmd_read))
    app.add_handler(CommandHandler("say", cmd_say))
    app.add_handler(CallbackQueryHandler(on_menu_click))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

async def start_http():
    await init_db()
    global application
    application = build_app()
    await application.initialize()
    await application.start()

    aio = web.Application()
    aio.router.add_get("/health", health)
    aio.router.add_post("/tgwebhook", tg_webhook)

    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)

    await set_menu(application)
    print("READY", flush=True)
    print("WEBHOOK:", f"{BASE_URL}/tgwebhook", flush=True)

    return aio

def run():
    loop = asyncio.get_event_loop()
    app = loop.run_until_complete(start_http())
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    run()
