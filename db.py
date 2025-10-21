import asyncpg
from typing import List, Dict
from config import DB_URL, LANG, MEM_LIMIT

async def db_conn():
    return await asyncpg.connect(DB_URL)

async def init_db():
    c = await db_conn()
    await c.execute(f"create table if not exists users (user_id bigint primary key, lang text default '{LANG}', persona text default 'assistant', voice boolean default true, translate_to text default null)")
    await c.execute("alter table users add column if not exists voicetrans boolean default false")
    await c.execute("create table if not exists memory (user_id bigint references users(user_id) on delete cascade, role text, content text, ts timestamptz default now())")
    await c.close()

async def get_user(uid: int) -> dict:
    c = await db_conn()
    row = await c.fetchrow("select user_id,lang,persona,voice,translate_to,voicetrans from users where user_id=$1", uid)
    if not row:
        await c.execute("insert into users(user_id) values($1)", uid)
        row = await c.fetchrow("select user_id,lang,persona,voice,translate_to,voicetrans from users where user_id=$1", uid)
    await c.close()
    d = dict(row)
    return {"user_id": d["user_id"], "lang": d["lang"], "persona": d["persona"], "voice": d["voice"], "translate_to": d["translate_to"], "voicetrans": d["voicetrans"]}

async def set_user(uid: int, **kw):
    if not kw:
        return
    fields = []
    vals = []
    for k, v in kw.items():
        fields.append(f"{k}=${len(vals)+1}")
        vals.append(v)
    c = await db_conn()
    await c.execute("update users set " + ", ".join(fields) + " where user_id=$" + str(len(vals)+1), *vals, uid)
    await c.close()

async def get_memory(uid: int) -> List[Dict[str, str]]:
    c = await db_conn()
    rows = await c.fetch("select role,content from memory where user_id=$1 order by ts asc", uid)
    await c.close()
    hist = [{"role": r["role"], "content": r["content"]} for r in rows]
    s = 0
    out = []
    for m in reversed(hist):
        s += len(m["content"].encode("utf-8"))
        out.append(m)
        if s > MEM_LIMIT:
            break
    return list(reversed(out))

async def add_memory(uid: int, role: str, content: str):
    c = await db_conn()
    await c.execute("insert into memory(user_id,role,content) values($1,$2,$3)", uid, role, content)
    await c.close()

async def reset_memory(uid: int):
    c = await db_conn()
    await c.execute("delete from memory where user_id=$1", uid)
    await c.close()
