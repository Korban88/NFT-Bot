# db.py
import os
import asyncpg
from typing import Optional, List, Dict, Any

DATABASE_URL = os.getenv("DATABASE_URL", "")

_pool: Optional[asyncpg.pool.Pool] = None

INIT_SQL = """
CREATE TABLE IF NOT EXISTS app_users (
    user_id BIGINT PRIMARY KEY,
    scanner_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS app_settings (
    id SMALLINT PRIMARY KEY DEFAULT 1,
    wallet_address TEXT
);

CREATE TABLE IF NOT EXISTS app_payments (
    id UUID PRIMARY KEY,
    user_id BIGINT NOT NULL,
    comment TEXT NOT NULL UNIQUE,
    amount_ton NUMERIC(32,9) NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    tx_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS app_payments_user_idx ON app_payments (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS app_scanner_settings (
    user_id BIGINT PRIMARY KEY,
    min_discount NUMERIC(5,2) NOT NULL DEFAULT 25.0,
    max_price_ton NUMERIC(32,9),
    collections TEXT[],
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- антидубликаты и лог по найденным сделкам
CREATE TABLE IF NOT EXISTS app_found_deals (
    deal_id TEXT PRIMARY KEY,           -- детерминированный хэш/идентификатор лота
    url TEXT UNIQUE,
    collection TEXT,
    name TEXT,
    price_ton NUMERIC(32,9),
    floor_ton NUMERIC(32,9),
    discount NUMERIC(6,2),
    seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

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
        await con.execute(INIT_SQL)

# --- Settings ---

async def set_wallet(pool: asyncpg.pool.Pool, address: str):
    async with pool.acquire() as con:
        await con.execute("""
            INSERT INTO app_settings (id, wallet_address)
            VALUES (1, $1)
            ON CONFLICT (id) DO UPDATE SET wallet_address = EXCLUDED.wallet_address
        """, address)

async def get_wallet(pool: asyncpg.pool.Pool) -> str:
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT wallet_address FROM app_settings WHERE id=1")
        return (row["wallet_address"] or "") if row else ""

# --- Scanner settings ---

async def get_or_create_scanner_settings(pool: asyncpg.pool.Pool, user_id: int) -> Dict[str, Any]:
    async with pool.acquire() as con:
        row = await con.fetchrow("""
            SELECT user_id, min_discount, max_price_ton, collections
            FROM app_scanner_settings WHERE user_id=$1
        """, user_id)
        if row:
            return dict(row)
        await con.execute("""
            INSERT INTO app_scanner_settings (user_id) VALUES ($1)
            ON CONFLICT (user_id) DO NOTHING
        """, user_id)
        return {"user_id": user_id, "min_discount": 25.0, "max_price_ton": None, "collections": None}

async def update_scanner_settings(pool: asyncpg.pool.Pool, user_id: int, **kwargs):
    if not kwargs:
        return
    fields = []
    values = []
    i = 1
    for k, v in kwargs.items():
        fields.append(f"{k} = ${i}")
        values.append(v)
        i += 1
    values.append(user_id)
    q = f"UPDATE app_scanner_settings SET {', '.join(fields)}, updated_at=NOW() WHERE user_id=${i}"
    async with (await get_pool()).acquire() as con:
        await con.execute(q, *values)

# --- Users ---

async def set_scanner_enabled(pool: asyncpg.pool.Pool, user_id: int, enabled: bool):
    async with pool.acquire() as con:
        await con.execute("""
            INSERT INTO app_users (user_id, scanner_enabled)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET scanner_enabled=$2, updated_at=NOW()
        """, user_id, enabled)

async def get_scanner_users(pool: asyncpg.pool.Pool) -> List[int]:
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT user_id FROM app_users WHERE scanner_enabled=TRUE")
        return [int(r["user_id"]) for r in rows]

# --- Deals ---

async def was_deal_seen(pool: asyncpg.pool.Pool, deal_id: str) -> bool:
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT 1 FROM app_found_deals WHERE deal_id=$1", deal_id)
        return row is not None

async def mark_deal_seen(pool: asyncpg.pool.Pool, deal: Dict[str, Any]):
    async with pool.acquire() as con:
        await con.execute("""
            INSERT INTO app_found_deals (deal_id, url, collection, name, price_ton, floor_ton, discount)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (deal_id) DO NOTHING
        """,
        deal.get("deal_id"),
        deal.get("url"),
        deal.get("collection"),
        deal.get("name"),
        deal.get("price_ton"),
        deal.get("floor_ton"),
        deal.get("discount"),
        )
