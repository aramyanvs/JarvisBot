import os
from typing import Any, Iterable, Optional
import psycopg
from psycopg.rows import dict_row

_DB_URL = os.getenv("DATABASE_URL") or os.getenv("DB_URL")
_pool: Optional[psycopg.AsyncConnectionPool] = None

async def init_db() -> None:
    global _pool
    if not _DB_URL:
        raise RuntimeError("DATABASE_URL is not set")
    if _pool is None:
        _pool = psycopg.AsyncConnectionPool(
            conninfo=_DB_URL,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        await _pool.open()

async def close_db() -> None:
    if _pool:
        await _pool.close()

async def fetchrow(query: str, *params: Any):
    if _pool is None:
        await init_db()
    async with _pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            return await cur.fetchone()

async def fetch(query: str, *params: Any):
    if _pool is None:
        await init_db()
    async with _pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            return await cur.fetchall()

async def execute(query: str, *params: Any) -> int:
    if _pool is None:
        await init_db()
    async with _pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            return cur.rowcount

async def executemany(query: str, seq_of_params: Iterable[Iterable[Any]]) -> int:
    if _pool is None:
        await init_db()
    async with _pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(query, seq_of_params)
            return cur.rowcount
