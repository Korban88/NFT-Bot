# db.py
import os
import asyncpg
from typing import Optional, List, Dict, Any

DATABASE_URL = os.getenv("DATABASE_URL", "")

_pool: Optional[asyncpg.pool.Pool] = None

async def get_pool() -> asyncpg.pool.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is empty")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        await init_db()
    return _pool

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            tx_hash TEXT UNIQUE NOT NULL,
            comment TEXT NOT NULL,
            amount_nano BIGINT NOT NULL,
            amount_ton DOUBLE PRECISION NOT NULL,
            cid TEXT,
            url TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        
async def record_payment(
    user_id: int,
    tx_hash: str,
    comment: str,
    amount_nano: int,
    amount_ton: float,
    cid: Optional[str],
    url: Optional[str],
) -> int:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow("""
            INSERT INTO payments (user_id, tx_hash, comment, amount_nano, amount_ton, cid, url)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (tx_hash) DO UPDATE
               SET cid=EXCLUDED.cid, url=EXCLUDED.url
            RETURNING id;
        """, user_id, tx_hash, comment, amount_nano, amount_ton, cid, url)
        return int(row["id"])

async def user_stats(user_id: int) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow("""
            SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_ton),0) AS sum_ton
            FROM payments WHERE user_id=$1;
        """, user_id)
        return {"count": int(row["cnt"]), "sum_ton": float(row["sum_ton"])}

async def user_last_payments(user_id: int, limit: int = 5) -> List[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch("""
            SELECT created_at, amount_ton, tx_hash, cid, url
            FROM payments
            WHERE user_id=$1
            ORDER BY created_at DESC
            LIMIT $2;
        """, user_id, limit)
        return rows
