import os, asyncio, io, re, tempfile
from dotenv import load_dotenv
load_dotenv()
from pyrogram import Client, filters
from pyrogram.types import Message, BotCommand
import asyncpg, httpx, pandas as pd
from readability import Document
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
from openai import OpenAI
from pdfminer.high_level import extract_text as pdf_text
from docx import Document as Docx
from aiohttp import web

API_ID = 27182880
API_HASH = "df9a668c2b864f047ffbe2f3cee898bf"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
DB_URL = os.getenv("DB_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
MEM_LIMIT = int(os.getenv("MEMORY_LIMIT", "1500"))
VOICE_MODE = os.getenv("VOICE_MODE", "true").lower() == "true"
LANG = os.getenv("LANGUAGE", "ru")
UA = "Mozilla/5.0"
SYS = f"Ты Jarvis — ассистент на {LANG}. Отвечай кратко и по делу. Если нужна свежая информация, используй сводку, приложенную в system."

app = Client("jarvis_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
oc = OpenAI(api_key=OPENAI_KEY)

async def db_conn(): return await asyncpg.connect(DB_URL)

async def init_db():
    c = await db_conn()
    await c.execute("create table if not exists users (user_id bigint primary key, memory jsonb default '[]'::jsonb)")
    await c.close()

async def get_memory(uid:int):
    c = await db_conn()
    r = await c.fetchrow("select memory from users where user_id=$1", uid)
    await c.close()
    return r["memory"] if r else []

async def save_memory(uid:int, mem):
    c = await db_conn()
    await c.execute("insert into users(user_id,memory) values($1,$2) on conflict(user_id) do update set memory=excluded.memory", uid, mem)
    await c.close()

async def reset_memory(uid:int):
    c = await db_conn()
    await c.execute("delete from users where user_id=$1", uid)
    await c.close()

def ask_openai(messages, temperature=0.3, max_tokens=800):
    r = oc.chat.completions.create(model=MODEL, messages=messages, temperature=temperature, max_tokens=max_tokens)
    return r.choices[0].message.content.strip()

async def fetch_url(url:str, limit=20000):
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent":UA}, timeout=25) as cl:
        r = await cl.get(url)
    ct = r.headers.get("content-type","").lower()
    if "text/html" in ct or "<html" in r.text[:500].lower():
        doc = Document(r.text); html = doc.summary()
        soup = BeautifulSoup(html,"lxml"); text = soup.get_text("\n", strip=True)
    else:
        text = r.text
    return re.sub(r"\n{3,}", "\n\n", text)[:limit]

def need_web(q:str):
    t = q.lower()
    keys = ["сейчас","сегодня","новост","курс","цена","сколько стоит","когда будет","последн","обнов","релиз","погода","расписан","матч","акции","доступно","вышел","итог"]
    if any(k in t for k in keys): return True
    if re.search(r"\b20(2[4-9]|3\d)\b", t): return True
    if "http://" in t or "https://" in t: return True
    return False

def extract_urls(q:str): return re.findall(r"https?://\S+", q)

async def fetch_urls(urls, limit_chars=12000):
    out = []
    for u in urls[:3]:
        try:
            t = await fetch_url(u, limit=4000)
            if t: out.append(t)
        except: pass
    return "\n\n".join(out)[:limit_chars]

async def search_and_fetch(query:str, hits:int=2, limit_chars:int=12000):
    links = []
    with DDGS() as ddg:
        for r in ddg.text(query, max_results=hits, safesearch="moderate"):
            if r and r.get("href"): links.append(r["href"])
    if not links: return ""
    return await fetch_urls(links, limit_chars=limit_chars)

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
        r = oc.audio.transcriptions.create(model="whisper-1", file=f)
    return r.text or ""

def tts_to_mp3(text:str):
    fn = tempfile.mktemp(suffix=".mp3")
    r = oc.audio.speech.create(model="tts-1", voice="alloy", input=text, format="mp3")
    with open(fn,"wb") as f: f.write(r.read())
    return fn

def cmds():
    return [
        BotCommand("ping","Проверка"),
        BotCommand("read","Прочитать сайт"),
        BotCommand("readfile","Прочитать файл"),
        BotCommand("summarize_file","Резюме файла"),
        BotCommand("translate_file","Перевод файла"),
        BotCommand("ask_file","Вопрос к файлу"),
        BotCommand("say","Ответ голосом"),
        BotCommand("reset","Сброс памяти"),
    ]

@app.on_message(filters.private & filters.command(["start"]))
async def on_start(c:Client, m:Message):
    await c.set_bot_commands(cmds())
    await m.reply_text("Готов. Меню установлено. Пиши вопрос.")

@app.on_message(filters.private & filters.command(["setmenu"]) & filters.user(ADMIN_ID))
async def on_setmenu(c:Client, m:Message):
    await c.set_bot_commands(cmds())
    await m.reply_text("Меню обновлено")

@app.on_message(filters.private & filters.command(["ping"]))
async def on_ping(c:Client, m:Message):
    await m.reply_text("pong")

@app.on_message(filters.private & filters.command(["reset"]) & filters.user(ADMIN_ID))
async def on_reset(c:Client, m:Message):
    await reset_memory(m.from_user.id)
    await m.reply_text("Память очищена")

@app.on_message(filters.private & filters.command(["read"]))
async def on_read(c:Client,m:Message):
    parts=(m.text or "").split(maxsplit=1)
    if len(parts)<2: return await m.reply_text("Формат: /read URL")
    try:
        raw=await fetch_url(parts[1])
    except Exception as e:
        return await m.reply_text(f"Ошибка: {e}")
    sys=[{"role":"system","content":"Суммаризируй текст кратко и структурировано."}]
    out=ask_openai(sys+[{"role":"user","content":raw[:16000]}]) if len(raw)>1800 else raw
    await m.reply_text(out[:4000])

@app.on_message(filters.private & filters.command(["readfile"]))
async def on_readfile(c:Client,m:Message):
    if not m.reply_to_message or not (m.reply_to_message.document or m.reply_to_message.audio or m.reply_to_message.voice):
        return await m.reply_text("Ответь на файл командой /readfile")
    f=m.reply_to_message.document or m.reply_to_message.audio or m.reply_to_message.voice
    p=await c.download_media(f)
    import asyncio as aio
    text=await aio.to_thread(read_any, p)
    await m.reply_text(text[:4000] or "пусто")

@app.on_message(filters.private & filters.command(["summarize_file"]))
async def on_summarize_file(c:Client,m:Message):
    if not m.reply_to_message or not m.reply_to_message.document:
        return await m.reply_text("Ответь на файл командой /summarize_file")
    p=await c.download_media(m.reply_to_message.document)
    import asyncio as aio
    text=await aio.to_thread(read_any, p)
    sys=[{"role":"system","content":"Суммаризируй текст кратко и структурировано."}]
    out=ask_openai(sys+[{"role":"user","content":text[:16000]}])
    await m.reply_text(out[:4000])

@app.on_message(filters.private & filters.command(["translate_file"]))
async def on_translate_file(c:Client,m:Message):
    parts=(m.text or "").split(maxsplit=1)
    if len(parts)<2: return await m.reply_text("Формат: /translate_file en (ответ на файл)")
    if not m.reply_to_message or not m.reply_to_message.document:
        return await m.reply_text("Ответь на файл командой /translate_file en")
    p=await c.download_media(m.reply_to_message.document)
    import asyncio as aio
    text=await aio.to_thread(read_any, p)
    sys=[{"role":"system","content":f"Переведи на язык: {parts[1].strip()}"}]
    out=ask_openai(sys+[{"role":"user","content":text[:16000]}])
    await m.reply_text(out[:4000])

@app.on_message(filters.private & filters.command(["ask_file"]))
async def on_ask_file(c:Client,m:Message):
    parts=(m.text or "").split(maxsplit=1)
    if len(parts)<2: return await m.reply_text("Формат: /ask_file вопрос (ответ на файл)")
    if not m.reply_to_message or not m.reply_to_message.document:
        return await m.reply_text("Ответь на файл командой /ask_file вопрос")
    q=parts[1]
    p=await c.download_media(m.reply_to_message.document)
    import asyncio as aio
    text=await aio.to_thread(read_any, p)
    msgs=[{"role":"system","content":"Отвечай только по тексту файла, кратко и точно."},{"role":"user","content":f"Текст файла:\n{text[:12000]}\n\nВопрос: {q}"}]
    ans=ask_openai(msgs,0.2,600)
    await m.reply_text(ans[:4000])

@app.on_message(filters.private & filters.voice)
async def on_voice(c:Client,m:Message):
    if not VOICE_MODE: return await m.reply_text("Голос отключен")
    p=await c.download_media(m.voice)
    txt=transcribe(p)
    await m.reply_text(txt or "пусто")

@app.on_message(filters.private & filters.command(["say"]))
async def on_say(c:Client,m:Message):
    if not VOICE_MODE: return await m.reply_text("Голос отключен")
    parts=(m.text or "").split(maxsplit=1)
    if len(parts)<2: return await m.reply_text("Формат: /say текст")
    mp3=tts_to_mp3(parts[1])
    try:
        await m.reply_audio(mp3)
    finally:
        try: os.remove(mp3)
        except: pass

@app.on_message(filters.private & ~filters.command(["start","setmenu","ping","reset","read","readfile","summarize_file","translate_file","ask_file","say"]))
async def on_chat(c:Client,m:Message):
    uid=m.from_user.id
    text=(m.text or m.caption or "").strip()
    if not text: return
    urls=extract_urls(text)
    web_snip=""
    if urls:
        try: web_snip=await fetch_urls(urls)
        except: web_snip=""
    elif need_web(text):
        try: web_snip=await search_and_fetch(text, hits=2)
        except: web_snip=""
    hist=await get_memory(uid)
    msgs=[{"role":"system","content":SYS}]
    if web_snip: msgs.append({"role":"system","content":"Актуальная сводка из интернета:\n"+web_snip})
    msgs+=hist+[{"role":"user","content":text}]
    reply=await asyncio.to_thread(ask_openai, msgs)
    hist.append({"role":"user","content":text})
    hist.append({"role":"assistant","content":reply})
    await save_memory(uid, hist[-MEM_LIMIT:])
    await m.reply_text(reply)

async def health(request): return web.Response(text="ok")

async def run_http_server():
    port = int(os.getenv("PORT", "8000"))
    app_http = web.Application()
    app_http.add_routes([web.get("/health", health)])
    runner = web.AppRunner(app_http)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    await init_db()
    await app.start()
    asyncio.create_task(run_http_server())
    print("READY")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
