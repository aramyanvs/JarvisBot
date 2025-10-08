import os, re, io, json, asyncio, tempfile
from dotenv import load_dotenv; load_dotenv()
import asyncpg, httpx, pandas as pd
from bs4 import BeautifulSoup
from readability import Document
from duckduckgo_search import DDGS
from aiohttp import web
from openai import OpenAI
from pdfminer.high_level import extract_text as pdf_text
from docx import Document as Docx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, BotCommand
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

OPENAI_KEY=os.getenv("OPENAI_API_KEY","")
DB_URL=os.getenv("DB_URL","")
BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
ADMIN_ID=int(os.getenv("ADMIN_ID","0"))
MODEL=os.getenv("OPENAI_MODEL","gpt-4o")
MEM_LIMIT=int(os.getenv("MEMORY_LIMIT","1500"))
LANG=os.getenv("LANGUAGE","ru")
BASE_URL=os.getenv("PUBLIC_URL","").rstrip("/")
PORT=int(os.getenv("PORT","10000"))
VOICE_MODE=os.getenv("VOICE_MODE","true").lower()=="true"
UA="Mozilla/5.0"
SYS=f"Ты Jarvis — ассистент на {LANG}. Отвечай кратко и по делу. Если нужна свежая информация, используй сводку из system."
oc=OpenAI(api_key=OPENAI_KEY)
application: Application|None=None

async def db_conn():
    return await asyncpg.connect(DB_URL)

async def init_db():
    c = await db_conn()
    try:
        await c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                memory JSONB DEFAULT '[]'::jsonb
            )
        """)
    finally:
        await c.close()

def _defaults(uid:int):
    return {"user_id":uid,"memory":[],"lang":LANG,"voice":"alloy","translate_to":LANG}

async def get_user(uid:int):
    c = await db_conn()
    try:
        row = await c.fetchrow("SELECT memory FROM users WHERE user_id=$1", uid)
    finally:
        await c.close()
    if not row:
        return _defaults(uid)
    v = row["memory"]
    if isinstance(v,str):
        try: v=json.loads(v) if v else []
        except: v=[]
    if isinstance(v,dict):
        mem=v.get("memory",[])
        lang=v.get("lang",LANG)
        voice=v.get("voice","alloy")
        tr=v.get("translate_to",LANG)
        return {"user_id":uid,"memory":mem,"lang":lang,"voice":voice,"translate_to":tr}
    return {"user_id":uid,"memory":(v or []),"lang":LANG,"voice":"alloy","translate_to":LANG}

async def save_user(uid:int, mem, lang:str, voice:str, tr:str):
    obj={"memory":mem,"lang":lang,"voice":voice,"translate_to":tr}
    mem_json=json.dumps(obj, ensure_ascii=False)
    c=await db_conn()
    try:
        await c.execute(
            """INSERT INTO users(user_id, memory)
               VALUES ($1, $2::jsonb)
               ON CONFLICT (user_id) DO UPDATE SET memory=excluded.memory""",
            uid, mem_json
        )
    finally:
        await c.close()

async def save_memory(uid:int, mem):
    u=await get_user(uid)
    await save_user(uid, mem, u["lang"], u["voice"], u["translate_to"])

def ask_openai(messages, temperature=0.3, max_tokens=800):
    r=oc.chat.completions.create(model=MODEL, messages=messages, temperature=temperature, max_tokens=max_tokens)
    return r.choices[0].message.content.strip()

async def fetch_url(url:str, limit=20000):
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent":UA}, timeout=25) as cl:
        r=await cl.get(url)
    ct=(r.headers.get("content-type") or "").lower()
    if "text/html" in ct or "<html" in r.text[:500].lower():
        html=Document(r.text).summary()
        soup=BeautifulSoup(html,"lxml")
        text=soup.get_text("\n", strip=True)
    else:
        text=r.text
    return re.sub(r"\n{3,}", "\n\n", text)[:limit]

def need_web(q:str):
    t=q.lower()
    keys=["сейчас","сегодня","новост","курс","цена","сколько стоит","когда будет","последн","обнов","релиз","погода","расписан","матч","акции","доступно","вышел","итог"]
    if any(k in t for k in keys): return True
    if re.search(r"\b20(2[4-9]|3\d)\b", t): return True
    if "http://" in t or "https://" in t: return True
    return False

def extract_urls(q:str): return re.findall(r"https?://\S+", q)

async def fetch_urls(urls, limit_chars=12000):
    out=[]
    for u in urls[:3]:
        try:
            t=await fetch_url(u, limit=4000)
            if t: out.append(t)
        except: pass
    return "\n\n".join(out)[:limit_chars]

async def search_and_fetch(query:str, hits:int=2, limit_chars:int=12000):
    links=[]
    try:
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=hits, safesearch="moderate"):
                if r and r.get("href"): links.append(r["href"])
    except: pass
    return await fetch_urls(links, limit_chars) if links else ""

def read_txt(p): return open(p,"r",encoding="utf-8",errors="ignore").read()
def read_pdf(p): return pdf_text(p) or ""
def read_docx(p): d=Docx(p); return "\n".join([x.text for x in d.paragraphs])
def read_table(p):
    if p.lower().endswith((".xlsx",".xls")): df=pd.read_excel(p)
    else: df=pd.read_csv(p)
    b=io.StringIO(); df.head(80).to_string(b); return b.getvalue()
def read_any(p):
    pl=p.lower()
    if pl.endswith((".txt",".md",".log")): return read_txt(p)
    if pl.endswith(".pdf"): return read_pdf(p)
    if pl.endswith(".docx"): return read_docx(p)
    if pl.endswith((".csv",".xlsx",".xls")): return read_table(p)
    return read_txt(p)

def transcribe(path:str):
    with open(path,"rb") as f:
        r=oc.audio.transcriptions.create(model="whisper-1", file=f)
    return (r.text or "").strip()

def tts_to_mp3(text:str, voice:str="alloy"):
    fn=tempfile.mktemp(suffix=".mp3")
    with oc.audio.speech.with_streaming_response.create(model="gpt-4o-mini-tts", voice=voice, input=text) as resp:
        resp.stream_to_file(fn)
    return fn

def detect_sentiment_simple(t:str):
    s=t.lower()
    neg=["устал","плохо","груст","не могу","тяжело","стресс","злюсь","боюсь","тревог"]
    pos=["класс","рад","супер","отлично","кайф","круто"]
    if any(w in s for w in neg): return "neg"
    if any(w in s for w in pos): return "pos"
    return "neu"

def empathy_prefix(mood:str):
    if mood=="neg": return "Понимаю. Давай решим это шаг за шагом. "
    if mood=="pos": return "Отлично! "
    return ""

def normalize_lang_name(name:str):
    m=name.strip().lower()
    map_ru={"русский":"ru","рус":"ru","ru":"ru","английский":"en","англ":"en","english":"en","en":"en","армянский":"hy","hy":"hy","немецкий":"de","de":"de","французский":"fr","fr":"fr","испанский":"es","es":"es","итальянский":"it","it":"it","китайский":"zh","zh":"zh","японский":"ja","ja":"ja","турецкий":"tr","tr":"tr"}
    return map_ru.get(m, m[:2])

def parse_translate_intent(text:str):
    p=re.compile(r"^(переведи|перевод|translate)\s+(на|to)\s+([a-zA-Zа-яА-Я\-]+)\s*[:\-]\s*(.+)$", re.IGNORECASE|re.DOTALL)
    m=p.match(text.strip())
    if not m: return None,None
    lang=normalize_lang_name(m.group(3))
    payload=m.group(4).strip()
    return lang, payload

def main_menu():
    rows=[
        [
            InlineKeyboardButton("☀️ Погода", callback_data="menu:weather"),
            InlineKeyboardButton("💸 Курс валют", callback_data="menu:currency")
        ],
        [
            InlineKeyboardButton("🌍 Новости", callback_data="menu:news"),
            InlineKeyboardButton("🧠 Факт", callback_data="menu:fact")
        ],
        [
            InlineKeyboardButton("⚙️ Настройки", callback_data="menu:settings")
        ]
    ]
    return InlineKeyboardMarkup(rows)

async def set_menu(app:Application):
    await app.bot.set_my_commands([
        BotCommand("start","запуск"),
        BotCommand("ping","проверка"),
        BotCommand("read","прочитать сайт"),
        BotCommand("say","озвучить текст"),
        BotCommand("reset","сбросить память"),
        BotCommand("weather","погода: /weather Москва"),
        BotCommand("currency","курс: /currency usd"),
        BotCommand("news","новости: /news запрос"),
        BotCommand("fact","случайный факт"),
        BotCommand("translate","перевод: /translate en Текст")
    ])

async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await set_menu(ctx.application)
    await update.message.reply_text("Привет, я Jarvis v2.2 Ultimate 🤖", reply_markup=main_menu())

async def on_button(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()
    d=q.data or ""
    if d=="menu:weather":
        await q.edit_message_text("Напиши: /weather Город")
    elif d=="menu:currency":
        await q.edit_message_text("Напиши: /currency usd (или eur, try, amd...)")
    elif d=="menu:news":
        await q.edit_message_text("Напиши: /news тема")
    elif d=="menu:fact":
        txt=await random_fact()
        await q.edit_message_text(txt or "Не нашёл факт, попробуй ещё.")
    elif d=="menu:settings":
        await q.edit_message_text("Доступно: /translate <lang> <текст> — разовый перевод голосом или текстом.")

async def cmd_ping(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_reset(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    await save_user(uid, [], LANG, "alloy", LANG)
    await update.message.reply_text("Память очищена.")

async def cmd_read(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    if len(parts)<2: return await update.message.reply_text("Формат: /read URL")
    try:
        raw=await fetch_url(parts[1])
    except Exception as e:
        return await update.message.reply_text(f"Ошибка: {e}")
    sys=[{"role":"system","content":"Суммаризируй текст кратко и структурировано."}]
    out=ask_openai(sys+[{"role":"user","content":raw[:16000]}]) if len(raw)>1800 else raw
    await update.message.reply_text(out[:4000])

async def cmd_say(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not VOICE_MODE: return await update.message.reply_text("Голос отключен")
    parts=(update.message.text or "").split(maxsplit=1)
    if len(parts)<2: return await update.message.reply_text("Формат: /say текст")
    mp3=tts_to_mp3(parts[1].strip(), "alloy")
    try:
        with open(mp3,"rb") as f:
            await update.message.reply_audio(InputFile(f, filename="jarvis.mp3"))
    finally:
        try: os.remove(mp3)
        except: pass

async def cmd_weather(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    if len(parts)<2: return await update.message.reply_text("Формат: /weather Город")
    city=parts[1].strip()
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            r=await cl.get(f"https://wttr.in/{city}?format=3")
        await update.message.reply_text(r.text.strip()[:4000])
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def cmd_currency(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    if len(parts)<2: return await update.message.reply_text("Формат: /currency usd")
    base=parts[1].strip().upper()
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            r=await cl.get(f"https://api.exchangerate.host/latest?base={base}&symbols=RUB,EUR,USD")
        data=r.json()
        rates=data.get("rates",{})
        txt=f"{base} → RUB: {rates.get('RUB')}\n{base} → USD: {rates.get('USD')}\n{base} → EUR: {rates.get('EUR')}"
        await update.message.reply_text(txt)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def cmd_news(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=(update.message.text or "").split(maxsplit=1)
    if len(q)<2: return await update.message.reply_text("Формат: /news запрос")
    web_snip=await search_and_fetch(q[1], hits=3)
    if not web_snip: return await update.message.reply_text("Ничего не нашёл.")
    msgs=[{"role":"system","content":"Суммаризируй факты списком, кратко."},{"role":"user","content":web_snip[:16000]}]
    s=ask_openai(msgs)
    await update.message.reply_text(s[:4000])

async def cmd_fact(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    s=await random_fact()
    await update.message.reply_text(s or "Не нашёл факт.")

async def random_fact():
    try:
        with DDGS() as ddg:
            res=list(ddg.text("интересный факт день", max_results=3, safesearch="moderate"))
        links=[r["href"] for r in res if r.get("href")]
        sn=await fetch_urls(links, 4000)
        if not sn: return ""
        msgs=[{"role":"system","content":"Выбери 1 короткий любопытный факт из текста и сформулируй на русском в 1-2 предложения."},{"role":"user","content":sn[:12000]}]
        return ask_openai(msgs)[:4000]
    except:
        return ""

async def on_document(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    f=update.message.document
    if not f: return
    tgfile=await ctx.bot.get_file(f.file_id)
    p=await tgfile.download_to_drive()
    txt=read_any(p)
    os.remove(p)
    msgs=[{"role":"system","content":"Суммаризируй документ кратко и по пунктам."},{"role":"user","content":txt[:16000]}]
    s=ask_openai(msgs)
    await update.message.reply_text(s[:4000])

async def on_voice(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not VOICE_MODE: return
    v=update.message.voice or update.message.audio
    if not v: return
    tgfile=await ctx.bot.get_file(v.file_id)
    p=await tgfile.download_to_drive()
    loop=asyncio.get_event_loop()
    text=await loop.run_in_executor(None, transcribe, p)
    try: os.remove(p)
    except: pass
    if not text:
        return await update.message.reply_text("Не удалось распознать голос.")
    lang_cmd, payload = parse_translate_intent(text)
    uid=update.effective_user.id
    u=await get_user(uid)
    hist=u["memory"]
    if lang_cmd and payload:
        tgt=lang_cmd
        msgs=[{"role":"system","content":"Переведи текст кратко и естественно."},{"role":"user","content":payload}]
        reply=ask_openai(msgs)
        mp3=tts_to_mp3(reply, u["voice"])
        try:
            with open(mp3,"rb") as f:
                await update.message.reply_audio(InputFile(f, filename="translate.mp3"))
        finally:
            try: os.remove(mp3)
            except: pass
        return
    web_snip=""
    if need_web(text):
        try: web_snip=await search_and_fetch(text, hits=2)
        except: web_snip=""
    msgs=[{"role":"system","content":SYS}]
    if web_snip: msgs.append({"role":"system","content":"Актуальная сводка из интернета:\n"+web_snip})
    msgs+=hist+[{"role":"user","content":text}]
    try:
        reply=await asyncio.to_thread(ask_openai, msgs)
    except Exception as e:
        reply=f"Ошибка модели: {e}"
    mood=detect_sentiment_simple(text)
    reply=empathy_prefix(mood)+reply
    hist.append({"role":"user","content":text})
    hist.append({"role":"assistant","content":reply})
    await save_user(uid, hist[-MEM_LIMIT:], u["lang"], u["voice"], u["translate_to"])
    mp3=tts_to_mp3(reply, u["voice"])
    try:
        with open(mp3,"rb") as f:
            await update.message.reply_audio(InputFile(f, filename="jarvis.mp3"))
    finally:
        try: os.remove(mp3)
        except: pass

async def on_text(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    t=(update.message.text or update.message.caption or "").strip()
    if not t: return
    if t.startswith("/translate"):
        parts=t.split(maxsplit=2)
        if len(parts)<3: return await update.message.reply_text("Формат: /translate en Текст")
        tgt=normalize_lang_name(parts[1])
        payload=parts[2]
        msgs=[{"role":"system","content":"Переведи текст естественно."},{"role":"user","content":payload}]
        reply=ask_openai(msgs)
        await update.message.reply_text(reply[:4000])
        return
    lang_cmd, payload=parse_translate_intent(t)
    if lang_cmd and payload:
        msgs=[{"role":"system","content":"Переведи текст естественно."},{"role":"user","content":payload}]
        reply=ask_openai(msgs)
        await update.message.reply_text(reply[:4000])
        return
    urls=extract_urls(t)
    web_snip=""
    if urls:
        try: web_snip=await fetch_urls(urls)
        except: web_snip=""
    elif need_web(t):
        try: web_snip=await search_and_fetch(t, hits=2)
        except: web_snip=""
    u=await get_user(uid)
    hist=u["memory"]
    msgs=[{"role":"system","content":SYS}]
    if web_snip: msgs.append({"role":"system","content":"Актуальная сводка из интернета:\n"+web_snip})
    msgs+=hist+[{"role":"user","content":t}]
    try:
        reply=await asyncio.to_thread(ask_openai, msgs)
    except Exception as e:
        reply=f"Ошибка модели: {e}"
    mood=detect_sentiment_simple(t)
    reply=empathy_prefix(mood)+reply
    hist.append({"role":"user","content":t})
    hist.append({"role":"assistant","content":reply})
    await save_user(uid, hist[-MEM_LIMIT:], u["lang"], u["voice"], u["translate_to"])
    await update.message.reply_text(reply[:4000])

async def health(request): return web.Response(text="ok")

async def tg_webhook(request):
    try:
        data=await request.json()
        upd=Update.de_json(data, application.bot)
        await application.process_update(upd)
        return web.Response(text="ok")
    except Exception as e:
        return web.Response(status=200, text=str(e))

def build_app()->Application:
    app=ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("read", cmd_read))
    app.add_handler(CommandHandler("say", cmd_say))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("currency", cmd_currency))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("fact", cmd_fact))
    app.add_handler(CallbackQueryHandler(on_button, pattern="^menu:"))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

async def main():
    global application
    await init_db()
    application=build_app()
    await application.initialize()
    await application.start()
    aio=web.Application()
    aio.add_routes([web.get("/health", health)])
    aio.add_routes([web.post("/tgwebhook", tg_webhook)])
    runner=web.AppRunner(aio); await runner.setup()
    site=web.TCPSite(runner, "0.0.0.0", PORT); await site.start()
    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)
    await set_menu(application)
    print("READY"); print("WEBHOOK:", f"{BASE_URL}/tgwebhook", flush=True)
    await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(main())
