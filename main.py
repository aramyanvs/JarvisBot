import os, io, re, json, base64, tempfile, asyncio, math, random
from dotenv import load_dotenv
load_dotenv()

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
BASE_URL=os.getenv("PUBLIC_URL","").rstrip("/")
PORT=int(os.getenv("PORT","10000"))

UA="Mozilla/5.0"
PERSONAS={
    "assistant":"Отвечай кратко и по делу, дружелюбно.",
    "professor":"Объясняй подробно, с примерами и структурой.",
    "sarcastic":"Отвечай остроумно и слегка саркастично, но без грубости.",
    "mentor":"Поддерживай и мотивируй, давай практические шаги."
}
DEFAULT_PERSONA="assistant"

oc=OpenAI(api_key=OPENAI_KEY)
application: Application|None=None

async def db_conn(): return await asyncpg.connect(DB_URL)

async def init_db():
    c=await db_conn()
    await c.execute("create table if not exists users (user_id bigint primary key, memory jsonb default '[]'::jsonb)")
    await c.execute("alter table users add column if not exists mode text default 'chat'")
    await c.execute("alter table users add column if not exists voice boolean default true")
    await c.execute("alter table users add column if not exists lang text default 'ru'")
    await c.execute("alter table users add column if not exists translate_to text default ''")
    await c.execute("alter table users add column if not exists persona text default 'assistant'")
    await c.close()

async def get_user(uid:int):
    c=await db_conn()
    r=await c.fetchrow("select user_id,memory,mode,voice,lang,translate_to,persona from users where user_id=$1", uid)
    await c.close()
    if not r:
        return {"user_id":uid,"memory":[],"mode":"chat","voice":True,"lang":"ru","translate_to":"","persona":DEFAULT_PERSONA}
    mem=r["memory"]
    if isinstance(mem,str):
        try: mem=json.loads(mem) if mem else []
        except: mem=[]
    return {"user_id":r["user_id"],"memory":mem or [],"mode":r["mode"],"voice":r["voice"],"lang":r["lang"],"translate_to":r["translate_to"],"persona":r["persona"]}

async def save_user(uid:int, memory, mode:str, voice:bool, lang:str, translate_to:str, persona:str):
    c=await db_conn()
    await c.execute(
        "insert into users(user_id,memory,mode,voice,lang,translate_to,persona) values($1,$2::jsonb,$3,$4,$5,$6,$7) "
        "on conflict(user_id) do update set memory=excluded.memory, mode=excluded.mode, voice=excluded.voice, lang=excluded.lang, translate_to=excluded.translate_to, persona=excluded.persona",
        uid, json.dumps(memory, ensure_ascii=False), mode, voice, lang, translate_to, persona
    )
    await c.close()

async def save_memory(uid:int, mem):
    u=await get_user(uid)
    await save_user(uid, mem, u["mode"], u["voice"], u["lang"], u["translate_to"], u["persona"])

def sys_prompt(lang:str, persona:str, mood:str="neutral"):
    base=f"Ты Jarvis. Язык интерфейса: {lang}. {PERSONAS.get(persona, PERSONAS[DEFAULT_PERSONA])}"
    if mood=="sad": base+=" Будь поддерживающим и тёплым."
    if mood=="angry": base+=" Сохраняй спокойствие и предлагай решения."
    if mood=="happy": base+=" Поддержи позитивный тон."
    return base

def detect_mood(text:str):
    t=text.lower()
    if any(k in t for k in ["устал","плохо","груст","тяжело","выжат","депрес"]): return "sad"
    if any(k in t for k in ["злю","бесит","раздраж","ярость"]): return "angry"
    if any(k in t for k in ["класс","супер","рад","отлично","ура"]): return "happy"
    return "neutral"

def need_web(q:str):
    t=q.lower()
    keys=["сейчас","сегодня","новост","курс","цена","сколько стоит","когда будет","последн","обнов","релиз","погода","расписан","матч","акции","доступно","вышел","итог","who won","today","price","weather","rate"]
    if any(k in t for k in keys): return True
    if re.search(r"\b20(2[4-9]|3\d)\b", t): return True
    if "http://" in t or "https://" in t: return True
    return False

def extract_urls(q:str): return re.findall(r"https?://\S+", q)

async def fetch_url(url:str, limit=20000):
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent":UA}, timeout=25) as cl:
        r=await cl.get(url)
    ct=(r.headers.get("content-type") or "").lower()
    if "pdf" in ct or url.lower().endswith(".pdf"):
        text=pdf_text(io.BytesIO(r.content))
    elif "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in ct or url.lower().endswith(".docx"):
        with tempfile.NamedTemporaryFile(delete=False,suffix=".docx") as f:
            f.write(r.content); p=f.name
        d=Docx(p); text="\n".join([x.text for x in d.paragraphs]); os.unlink(p)
    elif "text/html" in ct or "<html" in r.text[:500].lower():
        html=Document(r.text).summary()
        soup=BeautifulSoup(html,"lxml")
        text=soup.get_text("\n", strip=True)
    else:
        text=r.text
    return re.sub(r"\n{3,}", "\n\n", text)[:limit]

async def fetch_urls(urls, limit_chars=12000):
    out=[]
    for u in urls[:3]:
        try:
            t=await fetch_url(u, limit=4000)
            if t: out.append(t)
        except: pass
    return "\n\n".join(out)[:limit_chars]

async def ddg_search_text(q:str, n:int=3):
    res=[]
    try:
        with DDGS() as ddg:
            for r in ddg.text(q, max_results=n, safesearch="moderate"):
                if r and r.get("title"):
                    res.append({"title":r["title"],"href":r.get("href",""),"body":r.get("body","")})
    except: pass
    return res

async def search_and_fetch(query:str, hits:int=3, limit_chars:int=12000):
    links=[]
    try:
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=hits, safesearch="moderate"):
                if r and r.get("href"): links.append(r["href"])
    except: pass
    return await fetch_urls(links, limit_chars) if links else ""

def ask_openai(messages, temperature=0.3, max_tokens=700):
    r=oc.chat.completions.create(model=MODEL, messages=messages, temperature=temperature, max_tokens=max_tokens)
    return r.choices[0].message.content.strip()

def transcribe(path:str):
    with open(path,"rb") as f:
        r=oc.audio.transcriptions.create(model="whisper-1", file=f)
    return (r.text or "").strip()

def tts_to_mp3(text:str, voice:str="alloy"):
    fn=tempfile.mktemp(suffix=".mp3")
    with oc.audio.speech.with_streaming_response.create(model="gpt-4o-mini-tts", voice=voice, input=text) as resp:
        resp.stream_to_file(fn)
    return fn

async def weather_now(city:str):
    url=f"https://wttr.in/{city}?format=j1"
    async with httpx.AsyncClient(timeout=20) as cl:
        r=await cl.get(url)
    j=r.json()
    cur=j["current_condition"][0]
    return f"{city}: {cur['temp_C']}°C, {cur['weatherDesc'][0]['value']}, ветер {cur['windspeedKmph']} км/ч"

async def fx_rate(code:str, base:str="USD"):
    code=code.upper()
    async with httpx.AsyncClient(timeout=20) as cl:
        r=await cl.get(f"https://api.exchangerate.host/latest?base={base}")
    j=r.json()
    if code not in j.get("rates",{}): return f"Нет данных по {code}"
    val=j["rates"][code]
    return f"{base}->{code}: {val:.4f}"

async def news_brief():
    hits=await ddg_search_text("top news today", n=5)
    if not hits: return "Не нашёл свежих новостей."
    body="\n\n".join([f"{i+1}) {h['title']}\n{h['body']}" for i,h in enumerate(hits)])
    msgs=[{"role":"system","content":"Суммаризируй пункты кратко, по-русски, 5 тезисов."},{"role":"user","content":body[:12000]}]
    return ask_openai(msgs, max_tokens=400)

async def random_fact():
    msgs=[{"role":"system","content":"Сгенерируй один любопытный факт на русском, 1-2 предложения."},{"role":"user","content":"Дай факт"}]
    return ask_openai(msgs, max_tokens=80)

def make_chart(values_str:str):
    nums=[]
    for t in re.split(r"[,\s]+", values_str.strip()):
        if not t: continue
        try: nums.append(float(t))
        except: pass
    if not nums: raise ValueError("нет чисел")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fn=tempfile.mktemp(suffix=".png")
    plt.figure()
    plt.plot(range(1,len(nums)+1), nums)
    plt.title("Chart")
    plt.savefig(fn, bbox_inches="tight")
    plt.close()
    return fn

def main_menu():
    rows=[
        [InlineKeyboardButton("☀️ Погода", callback_data="menu_weather"), InlineKeyboardButton("💸 Курс", callback_data="menu_rate")],
        [InlineKeyboardButton("🌍 Новости", callback_data="menu_news"), InlineKeyboardButton("🧠 Факт", callback_data="menu_fact")],
        [InlineKeyboardButton("🧩 Персона", callback_data="menu_persona"), InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")]
    ]
    return InlineKeyboardMarkup(rows)

async def set_menu(app:Application):
    await app.bot.set_my_commands([
        BotCommand("start","запуск"),
        BotCommand("ping","проверка"),
        BotCommand("reset","сброс памяти"),
        BotCommand("read","прочитать URL"),
        BotCommand("say","озвучить текст"),
        BotCommand("weather","погода: /weather Москва"),
        BotCommand("rate","курс: /rate USD"),
        BotCommand("news","новости дня"),
        BotCommand("img","картинка: /img кот в очках"),
        BotCommand("chart","график: /chart 1,2,3"),
        BotCommand("persona","персона: /persona assistant")
    ])

async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await set_menu(ctx.application)
    await update.message.reply_text("Привет, я Jarvis Ultimate 🤖", reply_markup=main_menu())
    u=await get_user(update.effective_user.id)
    await save_user(u["user_id"], u["memory"], "chat", True, u["lang"], u["translate_to"], u["persona"])

async def cmd_ping(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_reset(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    u=await get_user(update.effective_user.id)
    await save_user(u["user_id"], [], u["mode"], u["voice"], u["lang"], "", u["persona"])
    await update.message.reply_text("Память очищена.")

async def on_button(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()
    d=q.data
    if d=="menu_weather": await q.edit_message_text("Напиши: /weather Город")
    elif d=="menu_rate": await q.edit_message_text("Напиши: /rate USD или /rate EUR")
    elif d=="menu_news": await q.edit_message_text(await news_brief(), reply_markup=main_menu())
    elif d=="menu_fact": await q.edit_message_text(await random_fact(), reply_markup=main_menu())
    elif d=="menu_persona": await q.edit_message_text("Доступно: assistant, professor, sarcastic, mentor\nИспользуй: /persona assistant")
    elif d=="menu_settings":
        u=await get_user(q.from_user.id)
        txt=f"Озвучка: {'вкл' if u['voice'] else 'выкл'}\nЯзык: {u['lang']}\nПеревод в голосовых: {u['translate_to'] or 'нет'}\nПерсона: {u['persona']}"
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("🔊 Переключить озвучку", callback_data="toggle_voice")],[InlineKeyboardButton("◀️ Назад", callback_data="back_menu")]])
        await q.edit_message_text(txt, reply_markup=kb)
    elif d=="toggle_voice":
        u=await get_user(q.from_user.id)
        await save_user(u["user_id"], u["memory"], u["mode"], not u["voice"], u["lang"], u["translate_to"], u["persona"])
        await q.edit_message_text(f"Озвучка теперь: {'вкл' if not u['voice'] else 'выкл'}", reply_markup=main_menu())
    elif d=="back_menu":
        await q.edit_message_text("Главное меню", reply_markup=main_menu())

async def cmd_weather(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    if len(parts)<2: return await update.message.reply_text("Формат: /weather Город")
    try:
        w=await weather_now(parts[1])
    except Exception as e:
        w=f"Ошибка погоды: {e}"
    await update.message.reply_text(w)

async def cmd_rate(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    if len(parts)<2: return await update.message.reply_text("Формат: /rate USD")
    try:
        r=await fx_rate(parts[1])
    except Exception as e:
        r=f"Ошибка курса: {e}"
    await update.message.reply_text(r)

async def cmd_news(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await news_brief())

async def cmd_img(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    if len(parts)<2: return await update.message.reply_text("Формат: /img описание")
    prompt=parts[1].strip()
    r=oc.images.generate(model="gpt-image-1", prompt=prompt, size="1024x1024")
    b64=r.data[0].b64_json
    img=base64.b64decode(b64)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
        f.write(img); p=f.name
    try:
        with open(p,"rb") as ph:
            await update.message.reply_photo(ph, caption="Готово")
    finally:
        try: os.remove(p)
        except: pass

async def cmd_chart(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    if len(parts)<2: return await update.message.reply_text("Формат: /chart 1,2,3,4")
    try:
        fn=make_chart(parts[1])
        with open(fn,"rb") as f:
            await update.message.reply_photo(f, caption="График")
    finally:
        try: os.remove(fn)
        except: pass

async def cmd_persona(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    if len(parts)<2: return await update.message.reply_text("Доступно: assistant, professor, sarcastic, mentor")
    p=parts[1].strip().lower()
    if p not in PERSONAS: return await update.message.reply_text("Неверная персона")
    u=await get_user(update.effective_user.id)
    await save_user(u["user_id"], u["memory"], u["mode"], u["voice"], u["lang"], u["translate_to"], p)
    await update.message.reply_text(f"Персона: {p}")

async def cmd_read(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    if len(parts)<2: return await update.message.reply_text("Формат: /read URL")
    url=parts[1].strip()
    try:
        raw=await fetch_url(url, limit=16000)
    except Exception as e:
        return await update.message.reply_text(f"Ошибка: {e}")
    if len(raw)<800:
        return await update.message.reply_text(raw[:4000])
    out=ask_openai([{"role":"system","content":"Суммаризируй текст кратко и структурировано на русском."},{"role":"user","content":raw[:14000]}], max_tokens=700)
    await update.message.reply_text(out[:4000])

async def cmd_say(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    parts=(update.message.text or "").split(maxsplit=1)
    if len(parts)<2: return await update.message.reply_text("Формат: /say текст")
    mp3=tts_to_mp3(parts[1].strip())
    try:
        with open(mp3,"rb") as f:
            await update.message.reply_audio(InputFile(f, filename="jarvis.mp3"))
    finally:
        try: os.remove(mp3)
        except: pass

async def on_voice(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    v=update.message.voice or update.message.audio
    if not v: return
    u=await get_user(update.effective_user.id)
    f=await ctx.bot.get_file(v.file_id)
    p=await f.download_to_drive()
    loop=asyncio.get_event_loop()
    text=await loop.run_in_executor(None, transcribe, p)
    try: os.remove(p)
    except: pass
    if not text: return await update.message.reply_text("Не удалось распознать голос.")
    mood=detect_mood(text)
    urls=extract_urls(text)
    web_snip=""
    if urls:
        try: web_snip=await fetch_urls(urls)
        except: web_snip=""
    elif need_web(text):
        try: web_snip=await search_and_fetch(text, hits=3)
        except: web_snip=""
    msgs=[{"role":"system","content":sys_prompt(u["lang"], u["persona"], mood)}]
    if web_snip: msgs.append({"role":"system","content":"Актуальная сводка:\n"+web_snip})
    msgs+=u["memory"]+[{"role":"user","content":text}]
    if re.search(r"(translate to|переведи на)\s+([a-zA-Zа-яА-Я\-]+)", text.lower()):
        m=re.search(r"(translate to|переведи на)\s+([a-zA-Zа-яА-Я\-]+)", text.lower())
        tgt=m.group(2)
        msgs=[{"role":"system","content":f"Переведи следующий текст и улучши формулировки. Язык перевода: {tgt}."},{"role":"user","content":text}]
    reply=ask_openai(msgs)
    u["memory"].append({"role":"user","content":text})
    u["memory"].append({"role":"assistant","content":reply})
    await save_user(u["user_id"], u["memory"][-1500:], u["mode"], u["voice"], u["lang"], u["translate_to"], u["persona"])
    mp3=tts_to_mp3(reply)
    try:
        with open(mp3,"rb") as f:
            await update.message.reply_voice(InputFile(f, filename="reply.mp3"))
    finally:
        try: os.remove(mp3)
        except: pass

async def on_text(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    u=await get_user(uid)
    t=(update.message.text or update.message.caption or "").strip()
    if not t: return
    if t.lower().startswith("translate to "):
        tgt=t[12:].strip()
        u["translate_to"]=tgt
        await save_user(u["user_id"], u["memory"], u["mode"], u["voice"], u["lang"], tgt, u["persona"])
        return await update.message.reply_text(f"Перевод голосовых → {tgt}")
    mood=detect_mood(t)
    urls=extract_urls(t)
    web_snip=""
    if urls:
        try: web_snip=await fetch_urls(urls)
        except: web_snip=""
    elif need_web(t):
        try: web_snip=await search_and_fetch(t, hits=3)
        except: web_snip=""
    msgs=[{"role":"system","content":sys_prompt(u["lang"], u["persona"], mood)}]
    if web_snip: msgs.append({"role":"system","content":"Актуальная сводка:\n"+web_snip})
    msgs+=u["memory"]+[{"role":"user","content":t}]
    if u["translate_to"]:
        msgs=[{"role":"system","content":f"Переведи на {u['translate_to']} и улучши стиль."},{"role":"user","content":t}]
    try:
        reply=ask_openai(msgs)
    except Exception as e:
        reply=f"Ошибка модели: {e}"
    u["memory"].append({"role":"user","content":t})
    u["memory"].append({"role":"assistant","content":reply})
    await save_user(uid, u["memory"][-1500:], u["mode"], u["voice"], u["lang"], u["translate_to"], u["persona"])
    await update.message.reply_text(reply)

WEBHOOK_URL=f"{BASE_URL}/tgwebhook" if BASE_URL else ""

async def tg_webhook(request):
    data=await request.json()
    upd=Update.de_json(data, application.bot)
    await application.process_update(upd)
    return web.Response(text="ok")

async def health(request): return web.Response(text="ok")

async def start_http():
    await init_db()
    global application
    application=ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("ping", cmd_ping))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(CommandHandler("read", cmd_read))
    application.add_handler(CommandHandler("say", cmd_say))
    application.add_handler(CommandHandler("weather", cmd_weather))
    application.add_handler(CommandHandler("rate", cmd_rate))
    application.add_handler(CommandHandler("news", cmd_news))
    application.add_handler(CommandHandler("img", cmd_img))
    application.add_handler(CommandHandler("chart", cmd_chart))
    application.add_handler(CommandHandler("persona", cmd_persona))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    await application.initialize()
    await application.start()
    app=web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/tgwebhook", tg_webhook)
    if WEBHOOK_URL:
        await application.bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    print("READY", flush=True)
    print("WEBHOOK:", WEBHOOK_URL, flush=True)
    return app

def run():
    loop=asyncio.get_event_loop()
    aio_app=loop.run_until_complete(start_http())
    web.run_app(aio_app, host="0.0.0.0", port=PORT)

if __name__=="__main__":
    run()
