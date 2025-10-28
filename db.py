import os, asyncpg, structlog

logger = structlog.get_logger()
DB_URL = os.getenv("DB_URL")
_pool = None

async def init_db():
    global _pool
    _pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=5)
    async with _pool.acquire() as c:
        await c.execute("""
        create table if not exists users(
            user_id bigint primary key,
            username text,
            first_name text,
            mode text default 'short'
        )""")
        await c.execute("""
        create table if not exists messages(
            id bigserial primary key,
            user_id bigint references users(user_id) on delete cascade,
            role text not null,
            content text not null,
            created_at timestamptz default now()
        )""")
        await c.execute("create index if not exists messages_user_idx on messages(user_id, created_at desc)")
    logger.info("db_ready")

async def close_db():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None

async def ensure_user(tg_user):
    async with _pool.acquire() as c:
        await c.execute(
            "insert into users(user_id, username, first_name) values($1,$2,$3) on conflict (user_id) do update set username=excluded.username, first_name=excluded.first_name",
            tg_user.id, tg_user.username, tg_user.first_name or ""
        )

async def save_message(user_id: int, role: str, content: str):
    async with _pool.acquire() as c:
        await c.execute("insert into messages(user_id, role, content) values($1,$2,$3)", user_id, role, content)

async def get_history(user_id: int, limit: int = 30):
    async with _pool.acquire() as c:
        rows = await c.fetch("select role, content from messages where user_id=$1 order by created_at asc limit $2", user_id, limit)
        return [(r["role"], r["content"]) for r in rows]

async def reset_history(user_id: int):
    async with _pool.acquire() as c:
        await c.execute("delete from messages where user_id=$1", user_id)

async def get_stats(user_id: int):
    async with _pool.acquire() as c:
        row = await c.fetchrow("select count(*) as cnt, coalesce(sum(length(content)),0) as chars from messages where user_id=$1", user_id)
        return int(row["cnt"]), int(row["chars"])

async def set_mode(user_id: int, mode: str):
    async with _pool.acquire() as c:
        await c.execute("update users set mode=$1 where user_id=$2", mode, user_id)

async def get_mode(user_id: int) -> str:
    async with _pool.acquire() as c:
        row = await c.fetchrow("select mode from users where user_id=$1", user_id)
        return row["mode"] if row and row["mode"] else "short"
