import os, re, io, json, tempfile, asyncio, math, random
from dotenv import load_dotenv
load_dotenv()
import asyncpg, httpx, pandas as pd
from bs4 import BeautifulSoup
from readability import Document as RDoc
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
MODEL=os.getenv("OPENAI_MODEL","gpt-4o-mini")
BASE_URL=os.getenv("PUBLIC_URL","").rstrip("/")
PORT=int(os.getenv("PORT","10000"))
MEM_LIMIT=int(os.getenv("MEMORY_LIMIT","1500"))
LANG_DEFAULT=os.getenv("LANGUAGE","ru")
VOICE_DEFAULT=os.getenv("VOICE_MODE","true").lower()=="true"
UA="Mozilla/5.0"

oc=OpenAI(api_key=OPENAI_KEY)
application: Application|None=None

async def db_conn(): return await asyncpg.connect(DB_URL)

async def init_db():
    c=await db_conn()
    await c.execute("""
    create table if not exists users(
        user_id bigint primary key,
        memory jsonb default '[]'::jsonb,
        mode text default 'assistant',
        voice boolean default true,
        lang text default $${}$$,
        translate_to text default ''
    )""".format(LANG_DEFAULT))
    await c.close()

async def get_user(uid:int):
    c=await db_conn()
    r=await c.fetchrow("select user_id,memory,mode,voice,lang,translate_to from users where user_id=$1", uid)
    await c.close()
    if not r:
        return {"user_id":uid,"memory":[],"mode":"assistant","voice":VOICE_DEFAULT,"lang":LANG_DEFAULT,"translate_to":""}
    mem=r["memory"]
    if isinstance(mem,str):
        try: mem=json.loads(mem) if mem else []
        except: mem=[]
    return {"user_id":r["user_id"],"memory":mem or [],"mode":r["mode"] or "assistant","voice":bool(r["voice"]),"lang":r["lang"] or LANG_DEFAULT,"translate_to":r["translate_to"] or ""}

async def save_user(uid:int, mem, mode:str, voice:bool, lang:str, translate_to:str):
    c=await db_conn()
    await c.execute("""
        insert into users(user_id,memory,mode,voice,lang,translate_to)
        values($1,$2::jsonb,$3,$4,$5,$6)
        on conflict(user_id) do update set
            memory=excluded.memory,
            mode=excluded.mode,
            voice=excluded.voice,
            lang=excluded.lang,
            translate_to=excluded.translate_to
    """, uid, json.dumps(mem, ensure_ascii=False), mode, voice, lang, translate_to)
    await c.close()

async def save_memory(uid:int, mem):
    c=await db_conn()
    await c.execute("update users set memory=$2::jsonb where user_id=$1", uid, json.dumps(mem, ensure_ascii=False))
    await c.close()

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("☀️ Погода", callback_data="menu_weather"),
         InlineKeyboardButton("💸 Курс", callback_data="menu_currency")],
        [InlineKeyboardButton("🌍 Новости", callback_data="menu_news"),
         InlineKeyboardButton("🧠 Факт", callback_data="menu_fact")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")]
    ])

def settings_menu(u):
    v_on="🔊 Вкл" if u["voice"] else "🔇 Выкл"
    lang_label="Русский" if (u["lang"] or LANG_DEFAULT).lower().startswith("ru") else "English"
    personality={"assistant":"Ассистент","professor":"Профессор","sarcastic":"Сарказм"}.get(u["mode"],"Ассистент")
    tr=u["translate_to"] or "—"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Язык: {lang_label}", callback_data="set_lang")],
        [InlineKeyboardButton(f"Голос: {v_on}", callback_data="toggle_voice")],
        [InlineKeyboardButton(f"Стиль: {personality}", callback_data="set_personality")],
        [InlineKeyboardButton(f"Перевод голосовых: {tr}", callback_data="set_translate")],
        [InlineKeyboardButton("← Назад", callback_data="back_main")]
    ])

def personality_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Ассистент", callback_data="pers_assistant")],
        [InlineKeyboardButton("🧙 Профессор", callback_data="pers_professor")],
        [InlineKeyboardButton("🐱 Сарказм", callback_data="pers_sarcastic")],
        [InlineKeyboardButton("Отмена", callback_data="menu_settings")]
    ])

def language_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Русский", callback_data="lang_ru"),
         InlineKeyboardButton("English", callback_data="lang_en")],
        [InlineKeyboardButton("Отмена", callback_data="menu_settings")]
    ])

def translate_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Русский", callback_data="tr_ru"),
         InlineKeyboardButton("English", callback_data="tr_en")],
        [InlineKeyboardButton("Deutsch", callback_data="tr_de"),
         InlineKeyboardButton("Español", callback_data="tr_es")],
        [InlineKeyboardButton("Français", callback_data="tr_fr"),
         InlineKeyboardButton("Italiano", callback_data="tr_it")],
        [InlineKeyboardButton("Выключить", callback_data="tr_off")],
        [InlineKeyboardButton("Отмена", callback_data="menu_settings")]
    ])

def sys_prompt(u):
    mood={"assistant":"Кратко и по делу.","professor":"Подробно, структурировано, с примерами.","sarcastic":"Кратко, умно, с лёгкой иронией, но уважительно."}.get(u["mode"],"Кратко и по делу.")
    lang=u["lang"] or LANG_DEFAULT
    return f"Ты Jarvis. Отвечай на {lang}. {mood}"

def ask_openai(messages, model=MODEL, temperature=0.3, max_tokens=800):
    r=oc.chat.completions.create(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
    return r.choices[0].message.content.strip()

async def http_get(url, expect_json=False, timeout=25):
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent":UA}, timeout=timeout) as cl:
        r=await cl.get(url)
    if expect_json: return r.json()
    return r.text

async def fetch_url(url:str, limit=20000):
    txt=await http_get(url)
    ct=""
    if "<html" in txt.lower()[:1000] or "</html>" in txt.lower()[-2000:]:
        html=RDoc(txt).summary()
        soup=BeautifulSoup(html, "lxml")
        text=soup.get_text("\n", strip=True)
    else:
        text=txt
    text=re.sub(r"\n{3,}", "\n\n", text)
    return text[:limit]

def extract_urls(q:str): return re.findall(r"https?://\S+", q)

def need_web(q:str):
    t=q.lower()
    keys=["сейчас","сегодня","новост","курс","цена","сколько стоит","когда будет","последн","обнов","релиз","погода","расписан","матч","акции","доступно","вышел","итог"]
    if any(k in t for k in keys): return True
    if re.search(r"\b20(2[4-9]|3\d)\b", t): return True
    if "http://" in t or "https://" in t: return True
    return False

async def fetch_urls(urls, limit_chars=12000):
    out=[]
    for u in urls[:3]:
        try:
            t=await fetch_url(u, limit=4000)
            if t: out.append(t)
        except: pass
    return "\n\n".join(out)[:limit_chars]

async def search_and_fetch(query:str, hits:int=3, limit_chars:int=12000):
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
    b=io.StringIO(); df.head(120).to_string(b); return b.getvalue()
def read_any(p):
    pl=p.lower()
    if pl.endswith((".txt",".md",".log")): return read_txt(p)
    if pl.endswith(".pdf"): return read_pdf(p)
    if pl.endswith(".docx"): return read_docx(p)
    if pl.endswith((".csv",".xlsx",".xls")): return read_table(p)
    return read_txt(p)

def detect_mood(text:str):
    t=text.lower()
    pos=len(re.findall(r"\bспасибо|\bкласс|\bсупер|\bура|\bотлично|\bрад",t))
    neg=len(re.findall(r"\bустал|\bплохо|\bтяжко|\bгруст|\bзол|\бесит|\bтревог",t))
    if neg>pos+1: return "sad"
    if pos>neg+1: return "happy"
    return "neutral"

def empathy_reply(text:str, mood:str, mode:str):
    if mood=="sad": return "Понимаю. Давай разгрузим голову. Хочешь, подсказки или план на ближайший шаг?"
    if mood=="happy": return "Рад слышать! Продолжим в том же духе. Чем помочь ещё?"
    if mode=="professor": return "Готов разложить по полочкам. Что уточнить?"
    return ""

def tts_to_mp3(text:str, voice="alloy"):
    fn=tempfile.mktemp(suffix=".mp3")
    with oc.audio.speech.with_streaming_response.create(model="gpt-4o-mini-tts", voice=voice, input=text) as resp:
        resp.stream_to_file(fn)
    return fn

def transcribe_to_text(path:str):
    with open(path,"rb") as f:
        r=oc.audio.transcriptions.create(model="whisper-1", file=f)
    return (r.text or "").strip()

async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    await ctx.application.bot.set_my_commands([
        BotCommand("start","запуск"),
        BotCommand("ping","проверка"),
        BotCommand("read","прочитать сайт"),
        BotCommand("reset","сбросить память"),
        BotCommand("news","топ-новости"),
        BotCommand("currency","курс по коду"),
        BotCommand("weather","погода"),
        BotCommand("fact","случайный факт"),
        BotCommand("tr","выбрать язык перевода голосовых")
    ])
    await update.message.reply_text("Привет, я Jarvis Ultimate PRO.", reply_markup=main_menu())

async def cmd_ping(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_reset(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    await save_user(u["user_id"], [], u["mode"], u["voice"], u["lang"], u["translate_to"])
    await update.message.reply_text("Память очищена.")

async def cmd_read(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    if len(parts)<2: return await update.message.reply_text("Формат: /read URL")
    try:
        raw=await fetch_url(parts[1])
    except Exception as e:
        return await update.message.reply_text(f"Ошибка: {e}")
    sys=[{"role":"system","content":"Суммаризируй кратко и структурировано."}]
    out=ask_openai(sys+[{"role":"user","content":raw[:16000]}]) if len(raw)>1800 else raw
    await update.message.reply_text(out[:4000])

async def cmd_news(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    try:
        txt=await search_and_fetch("site:news.google.com главные новости дня", hits=3)
    except:
        txt=""
    msgs=[{"role":"system","content":sys_prompt(u)},{"role":"user","content":"Сделай краткую сводку новостей по данным:\n"+(txt[:9000] if txt else "нет данных")}]
    out=ask_openai(msgs, max_tokens=600)
    await update.message.reply_text(out[:4000])

async def cmd_fact(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    msgs=[{"role":"system","content":sys_prompt(u)},{"role":"user","content":"Дай один любопытный факт дня, 2-3 предложения."}]
    out=ask_openai(msgs, max_tokens=150)
    await update.message.reply_text(out)

async def cmd_currency(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split()
    code=parts[1].upper() if len(parts)>1 else "USD"
    try:
        j=await http_get(f"https://api.exchangerate.host/latest?base={code}", expect_json=True)
        eur=j["rates"].get("EUR"); rub=j["rates"].get("RUB"); usd=j["rates"].get("USD"); gbp=j["rates"].get("GBP")
        s=f"1 {code} = {eur:.4f} EUR, {usd:.4f} USD, {gbp:.4f} GBP, {rub:.2f} RUB"
    except:
        s="Не удалось получить курс."
    await update.message.reply_text(s)

async def cmd_weather(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    city=parts[1] if len(parts)>1 else "Moscow"
    try:
        txt=await http_get(f"https://wttr.in/{city}?format=3")
    except:
        txt="Не удалось получить погоду."
    await update.message.reply_text(txt)

async def cmd_tr(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выбери язык перевода для голосовых:", reply_markup=translate_menu())

async def on_button(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    uid=q.from_user.id
    u=await get_user(uid)
    data=q.data
    if data=="menu_weather":
        await q.answer()
        await q.edit_message_text("Отправь /weather Город, например: /weather Moscow")
        return
    if data=="menu_currency":
        await q.answer()
        await q.edit_message_text("Отправь /currency USD или /currency EUR")
        return
    if data=="menu_news":
        await q.answer()
        await q.edit_message_text("Отправь /news для сводки.")
        return
    if data=="menu_fact":
        await q.answer()
        await q.edit_message_text("Отправь /fact для факта дня.")
        return
    if data=="menu_settings":
        await q.answer()
        await q.edit_message_text("Настройки:", reply_markup=settings_menu(u))
        return
    if data=="set_lang":
        await q.answer()
        await q.edit_message_text("Выбери язык интерфейса:", reply_markup=language_menu())
        return
    if data=="lang_ru":
        await q.answer("OK")
        u["lang"]="ru"; await save_user(uid,u["memory"],u["mode"],u["voice"],u["lang"],u["translate_to"])
        await q.edit_message_text("Язык: Русский", reply_markup=settings_menu(u))
        return
    if data=="lang_en":
        await q.answer("OK")
        u["lang"]="en"; await save_user(uid,u["memory"],u["mode"],u["voice"],u["lang"],u["translate_to"])
        await q.edit_message_text("Language: English", reply_markup=settings_menu(u))
        return
    if data=="toggle_voice":
        await q.answer("OK")
        u["voice"]=not u["voice"]; await save_user(uid,u["memory"],u["mode"],u["voice"],u["lang"],u["translate_to"])
        await q.edit_message_text("Настройки:", reply_markup=settings_menu(u))
        return
    if data=="set_personality":
        await q.answer()
        await q.edit_message_text("Выбери стиль:", reply_markup=personality_menu())
        return
    if data=="pers_assistant":
        await q.answer("OK")
        u["mode"]="assistant"; await save_user(uid,u["memory"],u["mode"],u["voice"],u["lang"],u["translate_to"])
        await q.edit_message_text("Стиль: Ассистент", reply_markup=settings_menu(u))
        return
    if data=="pers_professor":
        await q.answer("OK")
        u["mode"]="professor"; await save_user(uid,u["memory"],u["mode"],u["voice"],u["lang"],u["translate_to"])
        await q.edit_message_text("Стиль: Профессор", reply_markup=settings_menu(u))
        return
    if data=="pers_sarcastic":
        await q.answer("OK")
        u["mode"]="sarcastic"; await save_user(uid,u["memory"],u["mode"],u["voice"],u["lang"],u["translate_to"])
        await q.edit_message_text("Стиль: Сарказм", reply_markup=settings_menu(u))
        return
    if data=="set_translate":
        await q.answer()
        await q.edit_message_text("Выбери язык перевода голосовых:", reply_markup=translate_menu())
        return
    if data.startswith("tr_"):
        await q.answer("OK")
        lang=data.split("_",1)[1]
        u["translate_to"]="" if lang=="off" else lang
        await save_user(uid,u["memory"],u["mode"],u["voice"],u["lang"],u["translate_to"])
        label="—" if u["translate_to"]=="" else u["translate_to"]
        await q.edit_message_text(f"Перевод голосовых: {label}", reply_markup=settings_menu(u))
        return
    if data=="back_main":
        await q.answer()
        await q.edit_message_text("Главное меню:", reply_markup=main_menu())
        return

async def on_document(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    d=update.message.document
    if not d: return
    tf=tempfile.mktemp()
    f=await ctx.bot.get_file(d.file_id)
    await f.download_to_drive(custom_path=tf)
    try:
        raw=read_any(tf)
    except:
        raw=""
    try:
        msgs=[{"role":"system","content":sys_prompt(u)},{"role":"user","content":"Суммаризируй содержимое файла кратко:\n"+raw[:16000]}]
        reply=ask_openai(msgs, max_tokens=600)
    except Exception as e:
        reply=f"Ошибка обработки: {e}"
    hist=u["memory"]
    hist.append({"role":"user","content":"[документ]"})
    hist.append({"role":"assistant","content":reply})
    await save_user(uid, hist[-MEM_LIMIT:], u["mode"], u["voice"], u["lang"], u["translate_to"])
    await update.message.reply_text(reply[:4000])
    try: os.remove(tf)
    except: pass

async def on_voice(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    v=update.message.voice or update.message.audio
    if not v: return
    f=await ctx.bot.get_file(v.file_id)
    p=tempfile.mktemp(suffix=".ogg")
    await f.download_to_drive(custom_path=p)
    loop=asyncio.get_event_loop()
    text=await loop.run_in_executor(None, transcribe_to_text, p)
    text=text.strip()
    if not text:
        await update.message.reply_text("Не удалось распознать голос.")
        try: os.remove(p)
        except: pass
        return
    target=u["translate_to"]
    if target:
        msgs=[{"role":"system","content":sys_prompt(u)},{"role":"user","content":f"Переведи на {target} и сделай корректный литературный перевод:\n{text}"}]
        reply=ask_openai(msgs, max_tokens=600)
        mp3=tts_to_mp3(reply)
        try:
            with open(mp3,"rb") as f:
                await update.message.reply_audio(InputFile(f, filename="jarvis.mp3"))
        finally:
            try: os.remove(mp3)
            except: pass
        hist=u["memory"]
        hist.append({"role":"user","content":"[voice translate] "+text[:2000]})
        hist.append({"role":"assistant","content":reply})
        await save_user(uid, hist[-MEM_LIMIT:], u["mode"], u["voice"], u["lang"], u["translate_to"])
        try: os.remove(p)
        except: pass
        return
    urls=extract_urls(text)
    web_snip=""
    if urls:
        try: web_snip=await fetch_urls(urls)
        except: web_snip=""
    elif need_web(text):
        try: web_snip=await search_and_fetch(text, hits=3)
        except: web_snip=""
    msgs=[{"role":"system","content":sys_prompt(u)}]
    if web_snip: msgs.append({"role":"system","content":"Актуальная сводка:\n"+web_snip})
    msgs+=u["memory"]+[{"role":"user","content":text}]
    try:
        reply=ask_openai(msgs, max_tokens=800)
    except Exception as e:
        reply=f"Ошибка модели: {e}"
    hist=u["memory"]
    hist.append({"role":"user","content":text})
    hist.append({"role":"assistant","content":reply})
    await save_user(uid, hist[-MEM_LIMIT:], u["mode"], u["voice"], u["lang"], u["translate_to"])
    if u["voice"]:
        mp3=tts_to_mp3(reply)
        try:
            with open(mp3,"rb") as f:
                await update.message.reply_audio(InputFile(f, filename="jarvis.mp3"))
        finally:
            try: os.remove(mp3)
            except: pass
    else:
        await update.message.reply_text(reply[:4000])
    try: os.remove(p)
    except: pass

async def on_text(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    t=(update.message.text or update.message.caption or "").strip()
    if not t: return
    u=await get_user(uid)
    urls=extract_urls(t)
    web_snip=""
    if urls:
        try: web_snip=await fetch_urls(urls)
        except: web_snip=""
    elif need_web(t):
        try: web_snip=await search_and_fetch(t, hits=3)
        except: web_snip=""
    mood=detect_mood(t)
    emp=empathy_reply(t, mood, u["mode"])
    msgs=[{"role":"system","content":sys_prompt(u)}]
    if web_snip: msgs.append({"role":"system","content":"Актуальная сводка:\n"+web_snip})
    if emp: msgs.append({"role":"system","content":"Добавь эмпатию: "+emp})
    msgs+=u["memory"]+[{"role":"user","content":t}]
    try:
        reply=ask_openai(msgs, max_tokens=800)
    except Exception as e:
        reply=f"Ошибка модели: {e}"
    hist=u["memory"]
    hist.append({"role":"user","content":t})
    hist.append({"role":"assistant","content":reply})
    await save_user(uid, hist[-MEM_LIMIT:], u["mode"], u["voice"], u["lang"], u["translate_to"])
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

async def main():
    global application
    await init_db()
    application=ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("ping", cmd_ping))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(CommandHandler("read", cmd_read))
    application.add_handler(CommandHandler("news", cmd_news))
    application.add_handler(CommandHandler("fact", cmd_fact))
    application.add_handler(CommandHandler("currency", cmd_currency))
    application.add_handler(CommandHandler("weather", cmd_weather))
    application.add_handler(CommandHandler("tr", cmd_tr))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(MessageHandler(filters.Document.ALL, on_document))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    await application.initialize()
    await application.start()
    aio=web.Application()
    aio.add_routes([web.get("/health", health)])
    aio.add_routes([web.post("/tgwebhook", tg_webhook)])
    runner=web.AppRunner(aio); await runner.setup()
    site=web.TCPSite(runner, "0.0.0.0", PORT); await site.start()
    if BASE_URL:
        await application.bot.set_webhook(f"{BASE_URL}/tgwebhook", drop_pending_updates=True)
    await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(main())
