import os, ssl
import asyncpg

DB_URL = os.environ["DB_URL"]
_pool = None

async def init_db():
    global _pool
    ssl_ctx = ssl.create_default_context() if "sslmode=require" in DB_URL or "aivencloud.com" in DB_URL else None
    _pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=5, ssl=ssl_ctx)
    async with _pool.acquire() as c:
        await c.execute("""create table if not exists users(
            user_id bigint primary key,
            username text,
            full_name text,
            created_at timestamptz default now(),
            updated_at timestamptz default now()
        )""")
        await c.execute("""create table if not exists messages(
            id bigserial primary key,
            user_id bigint references users(user_id) on delete cascade,
            role text not null,
            content text not null,
            created_at timestamptz default now()
        )""")
        await c.execute("create index if not exists idx_messages_user_created on messages(user_id, created_at desc)")

async def save_user(user_id: int, username: str, full_name: str):
    async with _pool.acquire() as c:
        await c.execute(
            """insert into users(user_id, username, full_name) values($1,$2,$3)
               on conflict (user_id) do update set username=excluded.username, full_name=excluded.full_name, updated_at=now()""",
            user_id, username, full_name
        )

async def save_message(user_id: int, role: str, content: str):
    async with _pool.acquire() as c:
        await c.execute("insert into messages(user_id, role, content) values($1,$2,$3)", user_id, role, content)

async def fetch_context(user_id: int, limit: int = 10) -> list[tuple[str, str]]:
    async with _pool.acquire() as c:
        rows = await c.fetch("select role, content from messages where user_id=$1 order by created_at desc limit $2", user_id, limit)
        ordered = list(reversed([(r["role"], r["content"]) for r in rows]))
        return ordered
