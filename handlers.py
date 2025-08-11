# handlers.py
import asyncio
import os
import uuid
import base64
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Dict, Any, List
from urllib.parse import quote
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import httpx
import asyncpg
from aiogram import Dispatcher, types, Bot
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)

# ======== Config (общие) ========
ENV_WALLET = os.getenv("TON_WALLET_ADDRESS", "").strip()
TONAPI_KEY = os.getenv("TONAPI_KEY", "").strip()
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "").strip()
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or "0")
MIN_PAYMENT_TON = Decimal(os.getenv("MIN_PAYMENT_TON", "0.1"))
PAYMENT_TTL_MIN = int(os.getenv("PAYMENT_TTL_MIN", "30"))
# Всё время — по Москве
LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "Europe/Moscow")
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
TON_DECIMALS = Decimal(10**9)

# ======== Config (сканер) ========
LISTINGS_FEED_URL = os.getenv("LISTINGS_FEED_URL", "").strip()  # URL JSON массива лотов
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "60"))    # период фонового сканирования
SCAN_LOOKBACK_MIN = int(os.getenv("SCAN_LOOKBACK_MIN", "180"))   # брать лоты не старше N минут

# ======== Keyboards / Links ========
def main_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("Купить NFT"))
    kb.add(KeyboardButton("О коллекции"))
    kb.add(KeyboardButton("Мой профиль"))
    return kb

def build_ton_transfer_link(address: str, amount_ton: Decimal, comment: str) -> str:
    amount_nanotons = int(amount_ton * TON_DECIMALS)
    safe_comment = comment[:120]
    return f"ton://transfer/{address}?amount={amount_nanotons}&text={safe_comment}"

def build_tonkeeper_link(address: str, amount_ton: Decimal, comment: str) -> str:
    amount_nanotons = int(amount_ton * TON_DECIMALS)
    return f"https://app.tonkeeper.com/transfer/{address}?amount={amount_nanotons}&text={quote(comment)}"

def build_tonhub_link(address: str, amount_ton: Decimal, comment: str) -> str:
    amount_nanotons = int(amount_ton * TON_DECIMALS)
    return f"https://tonhub.com/transfer/{address}?amount={amount_nanotons}&text={quote(comment)}"

def pay_kb(ton_link: str, tk_link: str, th_link: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(text="Оплатить в TON (mobile)", url=ton_link))
    kb.add(InlineKeyboardButton(text="Tonkeeper (web)", url=tk_link))
    kb.add(InlineKeyboardButton(text="Tonhub (web)", url=th_link))
    return kb

# ======== DB helpers ========
CREATE_APP_PAYMENTS_SQL = """
CREATE TABLE IF NOT EXISTS app_payments (
    id UUID PRIMARY KEY,
    user_id BIGINT NOT NULL,
    comment TEXT NOT NULL,
    amount_ton NUMERIC(20,9) NOT NULL,
    status TEXT NOT NULL, -- pending | paid | expired | failed
    tx_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS app_payments_user_id_idx ON app_payments(user_id);
CREATE INDEX IF NOT EXISTS app_payments_comment_idx ON app_payments(comment);
"""

CREATE_APP_USERS_SQL = """
CREATE TABLE IF NOT EXISTS app_users (
    user_id BIGINT PRIMARY KEY,
    scanner_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_APP_CONFIG_SQL = """
CREATE TABLE IF NOT EXISTS app_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Сканер: настройки пользователя
CREATE_SCANNER_SETTINGS_SQL = """
CREATE TABLE IF NOT EXISTS app_scanner_settings (
    user_id BIGINT PRIMARY KEY,
    min_discount NUMERIC(6,2) NOT NULL DEFAULT 25.0,    -- % минимум скидки
    max_price_ton NUMERIC(20,9),                        -- максимум цена TON (NULL = нет лимита)
    collections TEXT[],                                 -- массив slug/id коллекций (NULL = любые)
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Сканер: отправленные айтемы, чтобы не дублировать
CREATE_SCANNER_SENT_SQL = """
CREATE TABLE IF NOT EXISTS app_scanner_sent (
    user_id BIGINT NOT NULL,
    item_id TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, item_id)
);
"""

async def ensure_tables(pool: asyncpg.Pool):
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(CREATE_APP_PAYMENTS_SQL)
            await con.execute(CREATE_APP_USERS_SQL)
            await con.execute(CREATE_APP_CONFIG_SQL)
            await con.execute(CREATE_SCANNER_SETTINGS_SQL)
            await con.execute(CREATE_SCANNER_SENT_SQL)

async def upsert_user(pool: asyncpg.Pool, user_id: int):
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO app_users (user_id) VALUES ($1) "
            "ON CONFLICT (user_id) DO UPDATE SET updated_at=now()",
            user_id,
        )

async def get_wallet(pool: asyncpg.Pool) -> str:
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT value FROM app_config WHERE key='wallet_address'")
    return row["value"] if row and row["value"] else ENV_WALLET

async def set_wallet(pool: asyncpg.Pool, address: str):
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO app_config (key, value, updated_at) VALUES ('wallet_address', $1, now()) "
            "ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=now()",
            address.strip()
        )

# ======== Address utils ========
def _b64url_decode_padded(s: str) -> bytes:
    s = s.replace(' ', '').strip()
    pad = (-len(s)) % 4
    s += "=" * pad
    return base64.urlsafe_b64decode(s)

def friendly_to_raw(addr: str) -> Optional[str]:
    try:
        b = _b64url_decode_padded(addr)
        if len(b) < 34:
            return None
        wc = int.from_bytes(b[1:2], "big", signed=True)
        hash_part = b[2:34]
        return f"{wc}:{hash_part.hex()}"
    except Exception:
        return None

def normalize_for_tonapi_local(addr: str) -> List[str]:
    variants: List[str] = []
    a = (addr or "").strip()
    if a:
        variants.append(a)
        raw = friendly_to_raw(a)
        if raw and raw not in variants:
            variants.append(raw)
    return variants

# ======== Providers: TonAPI / TonCenter (для платежей) ========
class TonAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base = "https://tonapi.io/v2"

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def health(self) -> bool:
        url = f"{self.base}/status"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, headers=self._headers())
                return r.status_code == 200
        except Exception:
            return False

    async def convert_address(self, address: str) -> Dict[str, str]:
        url = f"{self.base}/tools/convert_address?address={address}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, headers=self._headers())
            if r.status_code != 200:
                return {}
            data = r.json() or {}
            return {
                "bounceable": (data.get("bounceable") or {}).get("b64url") or "",
                "non_bounceable": (data.get("non_bounceable") or {}).get("b64url") or "",
                "raw": data.get("raw") or "",
            }
        except Exception:
            return {}

    async def fetch_tx(self, account_id: str, limit: int) -> Tuple[int, Optional[List[Dict[str, Any]]]]:
        url = f"{self.base}/accounts/{account_id}/transactions?limit={limit}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers=self._headers())
        if r.status_code == 200:
            data = r.json() or {}
            return r.status_code, (data.get("transactions", []) or data.get("items", []) or [])
        return r.status_code, None

class TonCenter:
    def __init__(self, api_key: str):
        self.base = "https://toncenter.com/api/v2/"
        self.api_key = api_key

    async def fetch_tx(self, address: str, limit: int) -> Tuple[int, Optional[List[Dict[str, Any]]]]:
        params = {"address": address, "limit": min(limit, 100), "archival": "true"}
        if self.api_key:
            params["api_key"] = self.api_key
        url = self.base + "getTransactions"
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, params=params)
        if r.status_code != 200:
            return r.status_code, None
        data = r.json() or {}
        if not data.get("ok"):
            return 200, []
        return 200, data.get("result") or []

# ======== Unified tx provider ========
class TxProvider:
    def __init__(self, tonapi: TonAPI, toncenter: TonCenter):
        self.tonapi = tonapi
        self.toncenter = toncenter

    async def list_recent(self, address: str, limit: int = 20) -> List[Dict[str, Any]]:
        for acc in normalize_for_tonapi_local(address):
            _, items = await self.tonapi.fetch_tx(acc, limit)
            if items is not None:
                return items
        forms = await self.tonapi.convert_address(address)
        for key in ["bounceable", "non_bounceable", "raw"]:
            acc = forms.get(key)
            if acc:
                _, items = await self.tonapi.fetch_tx(acc, limit)
                if items is not None:
                    return items
        for acc in normalize_for_tonapi_local(address):
            _, items = await self.toncenter.fetch_tx(acc, limit)
            if items is not None:
                return items
        for key in ["bounceable", "non_bounceable", "raw"]:
            acc = forms.get(key) if forms else None
            if acc:
                _, items = await self.toncenter.fetch_tx(acc, limit)
                if items is not None:
                    return items
        return []

# ======== Parsing helpers (tx) ========
def _to_ton(nanotons: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(nanotons)) / TON_DECIMALS
    except (InvalidOperation, TypeError):
        return None

def _extract_comment_from_msg(msg: Dict[str, Any]) -> str:
    for k in ["message", "comment"]:
        if msg.get(k):
            return str(msg.get(k)).strip()
    for path in [
        ("decoded_body", "text"),
        ("decoded", "body", "text"),
        ("msg_data", "text"),
        ("body", "text"),
    ]:
        cur: Any = msg
        ok = True
        for p in path:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                ok = False
                break
        if ok and isinstance(cur, (str, int, float)):
            return str(cur).strip()
    return ""

def _extract_amount_from_msg(msg: Dict[str, Any]) -> Optional[Decimal]:
    for candidate in [
        msg.get("value"),
        msg.get("amount"),
        (msg.get("msg_data") or {}).get("amount"),
        (msg.get("decoded_body") or {}).get("amount"),
        (msg.get("decoded") or {}).get("body", {}).get("amount"),
    ]:
        ton = _to_ton(candidate)
        if ton is not None:
            return ton
    return None

def _tx_id_str(tx: Dict[str, Any]) -> str:
    tid = tx.get("transaction_id")
    if isinstance(tid, dict):
        h = tid.get("hash"); lt = tid.get("lt")
        if h and lt: return f"{lt}:{h}"
        if h: return str(h)
        if lt: return str(lt)
    if isinstance(tx.get("hash"), (str, bytes)):
        return tx["hash"] if isinstance(tx["hash"], str) else tx["hash"].decode("utf-8", "ignore")
    if "lt" in tx and not isinstance(tx.get("lt"), dict):
        return str(tx.get("lt"))
    return f"tx-{uuid.uuid4().hex[:12]}"

# ---- Time helpers (MSK) ----
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)

def _fmt_local(dt: datetime) -> str:
    d = _to_local(dt)
    return d.strftime("%Y-%m-%d %H:%M:%S %Z")

# ======== Business logic: поиски платежей ========
async def find_incoming_with_comment(
    provider: TxProvider, address: str, comment: str,
    min_amount_ton: Decimal, lookback_minutes: int = 360
) -> Optional[Tuple[str, Decimal]]:
    since_dt = _now_utc() - timedelta(minutes=lookback_minutes)
    items = await provider.list_recent(address, limit=100)
    if not items:
        return None

    wanted = comment.strip().lower()

    for tx in items:
        utime = tx.get("utime") or tx.get("timestamp") or tx.get("now") or tx.get("created_at")
        tx_dt: Optional[datetime] = None
        if utime is not None:
            try:
                tx_dt = datetime.fromtimestamp(int(utime), tz=timezone.utc)
            except Exception:
                tx_dt = None
        if tx_dt and tx_dt < since_dt:
            continue

        tx_id = _tx_id_str(tx)

        msgs: List[Dict[str, Any]] = []
        for key in ["in_msg", "in_msg_desc", "inMessage", "inMessageDesc"]:
            if isinstance(tx.get(key), dict):
                msgs.append(tx[key])
        for key in ["out_msgs", "outMessages", "out_messages"]:
            if isinstance(tx.get(key), list):
                msgs.extend([m for m in tx[key] if isinstance(m, dict)])
        if isinstance(tx.get("messages"), list):
            msgs.extend([m for m in tx["messages"] if isinstance(m, dict)])

        for msg in msgs:
            cmt = _extract_comment_from_msg(msg).lower()
            amt = _extract_amount_from_msg(msg)
            if cmt == wanted and amt is not None and amt >= min_amount_ton:
                return tx_id, amt
    return None

# ======== Utils ========
def gen_comment() -> str:
    return f"pay-{uuid.uuid4().hex[:6]}"

async def notify_admin(bot: Bot, text: str):
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(ADMIN_CHAT_ID, text)
        except Exception:
            pass

# ======== Handlers: платежи/старт ========
async def start_handler(m: types.Message, pool: asyncpg.Pool):
    await upsert_user(pool, m.from_user.id)
    wa = await get_wallet(pool)
    text = (
        "Добро пожаловать в NFT бот.\n\n"
        "Доступные команды:\n"
        "/scanner_on — включить мониторинг лотов\n"
        "/scanner_off — выключить мониторинг\n"
        "/scanner_settings — показать мои фильтры\n"
        "/set_discount N — мин. скидка, %\n"
        "/set_maxprice N — макс. цена, TON\n"
        "/set_collections a,b,c — коллекции\n"
        "/scanner_test — прогнать сканер разово\n"
        "/pay — ссылка на оплату (есть web‑кнопки)\n"
        "/verify pay-xxxxxx — проверить оплату\n"
        "/payments — последние платежи\n"
        "/debug_tx — последние транзакции (оплата)\n"
        "/debug_addr — проверить формы адреса\n"
        "/set_wallet АДРЕС — сменить адрес приёма\n"
        "/health — проверить TonAPI\n\n"
        f"Текущий адрес приёма: {wa[:6]}…{wa[-6:] if wa else '—'}\n"
        f"Локальная TZ: {LOCAL_TZ_NAME} (MSK)"
    )
    await m.answer(text, reply_markup=main_kb())

async def health_handler(m: types.Message, provider: TxProvider, pool: asyncpg.Pool):
    ok = await provider.tonapi.health()
    wa = await get_wallet(pool)
    await m.answer(
        "Health:\n"
        f"TonAPI: {'ok' if ok else 'fail'}\n"
        f"Wallet: {wa[:6]}…{wa[-6:] if wa else '—'}"
    )

async def pay_handler(m: types.Message, bot: Bot, pool: asyncpg.Pool):
    wallet = await get_wallet(pool)
    if not wallet:
        await m.answer("Адрес приёма не задан. Укажи его командой: /set_wallet <адрес TON>")
        return

    comment = gen_comment()
    ton_link = build_ton_transfer_link(wallet, MIN_PAYMENT_TON, comment)
    tk_link = build_tonkeeper_link(wallet, MIN_PAYMENT_TON, comment)
    th_link = build_tonhub_link(wallet, MIN_PAYMENT_TON, comment)

    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO app_payments (id, user_id, comment, amount_ton, status) VALUES ($1,$2,$3,$4,'pending')",
            uuid.uuid4(), m.from_user.id, comment, float(MIN_PAYMENT_TON)
        )

    ttl_text = f"{PAYMENT_TTL_MIN} мин"
    msg = (
        "Оплата доступа/покупки.\n\n"
        f"Сумма: {MIN_PAYMENT_TON} TON или больше\n"
        f"Комментарий: `{comment}`\n"
        f"Адрес: `{wallet}`\n"
        f"Срок действия счёта: {ttl_text}\n\n"
        "Если ссылка ниже не открывается на ПК — используй кнопки Tonkeeper/Tonhub (web) "
        "или оплати вручную из @wallet: вставь адрес и комментарий как указано выше.\n\n"
        f"После оплаты запусти: `/verify {comment}`"
    )
    await m.answer(msg, parse_mode="Markdown", reply_markup=pay_kb(ton_link, tk_link, th_link))

    await notify_admin(bot, f"🧾 Новый счёт: user={m.from_user.id}, amount≥{float(MIN_PAYMENT_TON)} TON, comment={comment}")

async def verify_handler(m: types.Message, bot: Bot, provider: TxProvider, pool: asyncpg.Pool):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Укажи комментарий, например: `/verify pay-xxxxxx`", parse_mode="Markdown")
        return

    comment = parts[1].strip()
    wallet = await get_wallet(pool)
    if not wallet:
        await m.answer("Адрес приёма не задан. Укажи его: /set_wallet <адрес TON>")
        return

    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT id, status, created_at FROM app_payments WHERE user_id=$1 AND comment=$2 ORDER BY created_at DESC LIMIT 1",
            m.from_user.id, comment
        )

    if not row:
        await m.answer("Платёж с таким комментарием у тебя не найден. Создай новый через /pay.")
        return

    if row["status"] == "paid":
        await m.answer("Этот платёж уже подтверждён.")
        return

    found = await find_incoming_with_comment(provider, wallet, comment, MIN_PAYMENT_TON, lookback_minutes=360)

    created_at: datetime = row["created_at"]
    ttl_expired = (_now_utc() - created_at) > timedelta(minutes=PAYMENT_TTL_MIN)

    if not found:
        if ttl_expired:
            async with pool.acquire() as con:
                await con.execute("UPDATE app_payments SET status='expired' WHERE id=$1", row["id"])
            await m.answer("Срок действия счёта истёк. Создай новый через /pay.")
            return
        await m.answer("Платёж не найден. Если платил только что — подожди 1–2 минуты и повтори `/verify ...`.")
        return

    tx_id, amount_ton = found
    try:
        async with pool.acquire() as con:
            await con.execute(
                "UPDATE app_payments SET status='paid', tx_hash=$2, paid_at=now() WHERE id=$1",
                row["id"], str(tx_id)
            )
            await con.execute(
                "INSERT INTO app_users (user_id, scanner_enabled, updated_at) VALUES ($1, TRUE, now()) "
                "ON CONFLICT (user_id) DO UPDATE SET scanner_enabled=TRUE, updated_at=now()",
                m.from_user.id
            )
    except Exception as e:
        await m.answer(f"Оплата найдена ({amount_ton} TON), но возникла ошибка сохранения. Сообщи поддержку. Код: {e}")
        return

    now = _now_utc()
    receipt = (
        "Оплата подтверждена.\n"
        f"Сумма: {amount_ton} TON\n"
        f"Tx: {tx_id}\n"
        f"Дата: {_fmt_local(now)}\n\n"
        "Мониторинг лотов активирован. Команды: /scanner_settings, /scanner_off"
    )
    await m.answer(receipt)
    await notify_admin(bot, f"✅ Оплата подтверждена: user={m.from_user.id}, amount={amount_ton} TON, comment={comment}, tx={tx_id}")

async def payments_handler(m: types.Message, pool: asyncpg.Pool):
    async with pool.acquire() as con:
        rows = await con.fetch(
            "SELECT status, amount_ton, comment, tx_hash, created_at, paid_at "
            "FROM app_payments WHERE user_id=$1 ORDER BY created_at DESC LIMIT 5",
            m.from_user.id
        )
    if not rows:
        await m.answer("У тебя пока нет платежей.")
        return

    lines = ["Последние платежи:"]
    for r in rows:
        status = r["status"]
        amt = float(r["amount_ton"])
        cmt = r["comment"]
        txh = r["tx_hash"] or "—"
        created_local = _fmt_local(r["created_at"])
        paid_local = _fmt_local(r["paid_at"]) if r["paid_at"] else "—"
        lines.append(
            f"• {status} | {amt:.3f} TON | {cmt}\n"
            f"  tx: {txh}\n"
            f"  created: {created_local} | paid: {paid_local}"
        )

    await m.answer("\n".join(lines))

# ======== Сканер: хранение/фильтры/уведомления ========
async def get_or_create_scanner_settings(pool: asyncpg.Pool, user_id: int) -> Dict[str, Any]:
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT min_discount, max_price_ton, collections FROM app_scanner_settings WHERE user_id=$1", user_id)
        if row:
            return dict(row)
        await con.execute(
            "INSERT INTO app_scanner_settings (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
            user_id
        )
        return {"min_discount": Decimal("25.0"), "max_price_ton": None, "collections": None}

async def update_scanner_settings(pool: asyncpg.Pool, user_id: int, **kwargs):
    fields = []
    values = []
    for k, v in kwargs.items():
        fields.append(f"{k}=$%d" % (len(values)+1))
        values.append(v)
    if not fields:
        return
    q = "UPDATE app_scanner_settings SET " + ", ".join(fields) + ", updated_at=now() WHERE user_id=$%d" % (len(values)+1)
    values.append(user_id)
    async with pool.acquire() as con:
        await con.execute(q, *values)

async def mark_sent(pool: asyncpg.Pool, user_id: int, item_id: str) -> bool:
    try:
        async with pool.acquire() as con:
            await con.execute(
                "INSERT INTO app_scanner_sent (user_id, item_id) VALUES ($1,$2)",
                user_id, item_id
            )
        return True
    except Exception:
        return False  # уже отправляли

def _item_discount(item: Dict[str, Any]) -> Optional[Decimal]:
    if item.get("discount") is not None:
        try:
            return Decimal(str(item["discount"]))
        except InvalidOperation:
            pass
    price = item.get("price_ton")
    floor = item.get("floor_ton")
    try:
        if price is not None and floor is not None and float(floor) > 0:
            return Decimal((float(floor) - float(price)) / float(floor) * 100.0)
    except Exception:
        pass
    return None

def _item_fresh_enough(item: Dict[str, Any]) -> bool:
    ts = item.get("listed_at")
    if not ts:
        return True
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return (_now_utc() - dt) <= timedelta(minutes=SCAN_LOOKBACK_MIN)
    except Exception:
        return True

def _fmt_item_caption(it: Dict[str, Any]) -> str:
    name = it.get("name") or "NFT"
    coll = it.get("collection") or "—"
    price = it.get("price_ton")
    floor = it.get("floor_ton")
    disc = _item_discount(it)
    url = it.get("url") or "—"
    lines = [f"🔥 {name}", f"Коллекция: {coll}"]
    if price is not None:
        lines.append(f"Цена: {float(price):.3f} TON")
    if floor is not None:
        lines.append(f"Floor: {float(floor):.3f} TON")
    if disc is not None:
        lines.append(f"Скидка: {float(disc):.1f}%")
    lines.append(url)
    return "\n".join(lines)

def _item_matches(it: Dict[str, Any], st: Dict[str, Any]) -> bool:
    if not _item_fresh_enough(it):
        return False
    disc = _item_discount(it)
    if disc is None or Decimal(disc) < Decimal(st["min_discount"]):
        return False
    if st.get("max_price_ton") is not None:
        try:
            if Decimal(str(it.get("price_ton", "0"))) > Decimal(st["max_price_ton"]):
                return False
        except InvalidOperation:
            return False
    cols = st.get("collections")
    if cols:
        try:
            cols = [c.strip().lower() for c in cols]
            item_col = (it.get("collection") or "").strip().lower()
            if item_col and item_col not in cols:
                return False
        except Exception:
            pass
    return True

async def send_item_alert(bot: Bot, user_id: int, item: Dict[str, Any]):
    image = item.get("image")
    caption = _fmt_item_caption(item)
    if image and image.startswith("http"):
        try:
            await bot.send_photo(user_id, photo=image, caption=caption)
            return
        except Exception:
            pass
    await bot.send_message(user_id, caption)

# ======== Сканер: загрузка фида ========
async def fetch_listings() -> List[Dict[str, Any]]:
    if not LISTINGS_FEED_URL:
        return []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(LISTINGS_FEED_URL)
        if r.status_code != 200:
            return []
        data = r.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data["items"]
        return []
    except Exception:
        return []

# ======== Сканер: фоновая задача ========
async def scanner_loop(bot: Bot, pool: asyncpg.Pool):
    await asyncio.sleep(3)  # дать боту стартануть
    while True:
        try:
            items = await fetch_listings()
            if items:
                async with pool.acquire() as con:
                    users = await con.fetch("SELECT user_id FROM app_users WHERE scanner_enabled=TRUE")
                for u in users:
                    uid = u["user_id"]
                    st = await get_or_create_scanner_settings(pool, uid)
                    for it in items:
                        item_id = str(it.get("id") or f"{it.get('collection','')}-{it.get('name','')}-{it.get('url','')}")
                        if not item_id:
                            continue
                        if not _item_matches(it, st):
                            continue
                        inserted = await mark_sent(pool, uid, item_id)
                        if not inserted:
                            continue
                        await send_item_alert(bot, uid, it)
            await asyncio.sleep(SCAN_INTERVAL_SEC)
        except Exception:
            await asyncio.sleep(SCAN_INTERVAL_SEC)

# ======== Handlers: сканер команды ========
async def scanner_on_handler(m: types.Message, pool: asyncpg.Pool):
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO app_users (user_id, scanner_enabled) VALUES ($1, TRUE) "
            "ON CONFLICT (user_id) DO UPDATE SET scanner_enabled=TRUE, updated_at=now()",
            m.from_user.id
        )
    await m.answer("Мониторинг включён. Уведомлю о выгодных лотах.", reply_markup=main_kb())

async def scanner_off_handler(m: types.Message, pool: asyncpg.Pool):
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE app_users SET scanner_enabled=FALSE, updated_at=now() WHERE user_id=$1",
            m.from_user.id
        )
    await m.answer("Мониторинг выключен.", reply_markup=main_kb())

async def scanner_settings_handler(m: types.Message, pool: asyncpg.Pool):
    st = await get_or_create_scanner_settings(pool, m.from_user.id)
    min_disc_text = f"{float(st['min_discount']):.1f}%"
    max_price_text = "нет" if st["max_price_ton"] is None else f"{float(st['max_price_ton']):.3f} TON"
    cols_text = ", ".join(st["collections"]) if st["collections"] else "любой"

    lines = [
        "Текущие фильтры сканера:",
        f"— Мин. скидка: {min_disc_text}",
        f"— Макс. цена: {max_price_text}",
        f"— Коллекции: {cols_text}",
        "",
        "Команды настройки:",
        "/set_discount N",
        "/set_maxprice N",
        "/set_collections col1,col2"
    ]
    await m.answer("\n".join(lines), reply_markup=main_kb())

async def set_discount_handler(m: types.Message, pool: asyncpg.Pool):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Использование: /set_discount 25")
        return
    try:
        val = Decimal(parts[1].strip())
        if val < 0 or val > 100:
            raise InvalidOperation
    except Exception:
        await m.answer("Нужно число от 0 до 100.")
        return
    await update_scanner_settings(pool, m.from_user.id, min_discount=val)
    await m.answer(f"Мин. скидка обновлена: {float(val):.1f}%")

async def set_maxprice_handler(m: types.Message, pool: asyncpg.Pool):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Использование: /set_maxprice 100")
        return
    txt = parts[1].strip().lower()
    if txt in ("-", "none", "нет"):
        await update_scanner_settings(pool, m.from_user.id, max_price_ton=None)
        await m.answer("Макс. цена очищена (нет лимита).")
        return
    try:
        val = Decimal(txt)
        if val <= 0:
            raise InvalidOperation
    except Exception:
        await m.answer("Нужно положительное число (TON) или 'none'.")
        return
    await update_scanner_settings(pool, m.from_user.id, max_price_ton=val)
    await m.answer(f"Макс. цена обновлена: {float(val):.3f} TON")

async def set_collections_handler(m: types.Message, pool: asyncpg.Pool):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Использование: /set_collections col1,col2  (пусто/none — любые)")
        return
    raw = parts[1].strip()
    if raw.lower() in ("none", "любой", "любые", "-", ""):
        await update_scanner_settings(pool, m.from_user.id, collections=None)
        await m.answer("Список коллекций очищен: любые коллекции.")
        return
    cols = [c.strip() for c in raw.split(",") if c.strip()]
    if not cols:
        await update_scanner_settings(pool, m.from_user.id, collections=None)
        await m.answer("Список коллекций очищен: любые коллекции.")
        return
    await update_scanner_settings(pool, m.from_user.id, collections=cols)
    await m.answer(f"Коллекции обновлены: {', '.join(cols)}")

async def scanner_test_handler(m: types.Message, bot: Bot, pool: asyncpg.Pool):
    items = await fetch_listings()
    if not items:
        await m.answer("Фид пуст или недоступен (LISTINGS_FEED_URL).")
        return
    st = await get_or_create_scanner_settings(pool, m.from_user.id)
    sent = 0
    for it in items[:20]:
        if _item_matches(it, st):
            await send_item_alert(bot, m.from_user.id, it)
            sent += 1
    if sent == 0:
        await m.answer("Подходящих лотов не найдено по текущим фильтрам.")
    else:
        await m.answer(f"Отправлено лотов: {sent}")

# ======== Debug: платежные / адрес ========
async def debug_tx_handler(m: types.Message, provider: TxProvider, pool: asyncpg.Pool):
    wallet = await get_wallet(pool)
    if not wallet:
        await m.answer("Адрес приёма не задан. Укажи его: /set_wallet <адрес TON>")
        return
    items = await provider.list_recent(wallet, limit=10)
    if not items:
        await m.answer("Нет данных по транзакциям. Проверь адрес/задержку сети или сделай недавний перевод.")
        return

    lines: List[str] = ["Последние транзакции:"]
    shown = 0
    for tx in items:
        if shown >= 5:
            break
        tx_id = _tx_id_str(tx)
        utime = tx.get("utime") or tx.get("timestamp") or tx.get("now") or tx.get("created_at")
        when = "—"
        if utime is not None:
            try:
                dt_utc = datetime.fromtimestamp(int(utime), tz=timezone.utc)
                when = _fmt_local(dt_utc)
            except Exception:
                when = "—"

        msgs: List[Dict[str, Any]] = []
        for key in ["in_msg", "in_msg_desc"]:
            if isinstance(tx.get(key), dict):
                msgs.append(tx[key])
        for key in ["out_msgs"]:
            if isinstance(tx.get(key), list):
                msgs.extend([m for m in tx[key] if isinstance(m, dict)])
        if isinstance(tx.get("messages"), list):
            msgs.extend([m for m in tx["messages"] if isinstance(m, dict)])

        found_any = False
        for msg in msgs:
            cmt = _extract_comment_from_msg(msg)
            amt = _extract_amount_from_msg(msg)
            if cmt or amt is not None:
                lines.append(f"• {when} | {tx_id}\n  comment: {cmt or '—'}\n  amount: {amt if amt is not None else '—'} TON")
                found_any = True
        if not found_any:
            lines.append(f"• {when} | {tx_id}\n  (нет комм/суммы в доступных полях)")
        shown += 1

    await m.answer("\n".join(lines))

async def debug_addr_handler(m: types.Message, provider: TxProvider, pool: asyncpg.Pool):
    wallet = await get_wallet(pool)
    if not wallet:
        await m.answer("Адрес приёма не задан. Укажи его: /set_wallet <адрес TON>")
        return

    lines: List[str] = [f"Исходный: {wallet}", "Проверка форм адреса:"]
    for v in normalize_for_tonapi_local(wallet):
        code, items = await provider.tonapi.fetch_tx(v, limit=1)
        lines.append(f"— TonAPI {v} -> HTTP {code}, items={'yes' if items else 'no'}")
    forms = await provider.tonapi.convert_address(wallet)
    for key in ["bounceable", "non_bounceable", "raw"]:
        v = forms.get(key)
        if v:
            code, items = await provider.tonapi.fetch_tx(v, limit=1)
            lines.append(f"— TonAPI {key}: {v} -> HTTP {code}, items={'yes' if items else 'no'}")
    for v in normalize_for_tonapi_local(wallet):
        code, items = await provider.toncenter.fetch_tx(v, limit=1)
        lines.append(f"— TONCENTER {v} -> HTTP {code}, items={'yes' if items else 'no'}")
    for key in ["bounceable", "non_bounceable", "raw"]:
        v = forms.get(key) if forms else None
        if v:
            code, items = await provider.toncenter.fetch_tx(v, limit=1)
            lines.append(f"— TONCENTER {key}: {v} -> HTTP {code}, items={'yes' if items else 'no'}")

    await m.answer("\n".join(lines))

async def set_wallet_handler(m: types.Message, pool: asyncpg.Pool):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Использование: /set_wallet <адрес TON>")
        return
    address = parts[1].strip()
    if len(address) < 20:
        await m.answer("Похоже на невалидный адрес. Пришли корректный TON-адрес.")
        return
    await set_wallet(pool, address)
    await m.answer(f"Ок! Адрес приёма обновлён: {address[:6]}…{address[-6:]}")

async def profile_handler(m: types.Message, pool: asyncpg.Pool):
    async with pool.acquire() as con:
        total_paid = await con.fetchval(
            "SELECT COALESCE(SUM(amount_ton),0) FROM app_payments WHERE user_id=$1 AND status='paid'",
            m.from_user.id
        )
        last_tx = await con.fetchrow(
            "SELECT amount_ton, paid_at FROM app_payments WHERE user_id=$1 AND status='paid' ORDER BY paid_at DESC LIMIT 1",
            m.from_user.id
        )
        enabled = await con.fetchval("SELECT scanner_enabled FROM app_users WHERE user_id=$1", m.from_user.id)

    txt = [
        f"Сканер: {'включён' if enabled else 'выключен'}",
        f"Суммарно оплачено: {float(total_paid):.3f} TON",
        "Последняя оплата:"
    ]
    if last_tx:
        txt.append(f"— {float(last_tx['amount_ton']):.3f} TON, {_fmt_local(last_tx['paid_at'])}")
    else:
        txt.append("— пока пусто")
    await m.answer("\n".join(txt))

# ======== Router ========
def register_handlers(dp: Dispatcher, bot: Bot, pool: asyncpg.Pool):
    tonapi = TonAPI(TONAPI_KEY)
    toncenter = TonCenter(TONCENTER_API_KEY)
    provider = TxProvider(tonapi, toncenter)

    loop = asyncio.get_event_loop()
    loop.create_task(ensure_tables(pool))
    # фоновый сканер
    loop.create_task(scanner_loop(bot, pool))

    # общие/платёжные
    dp.register_message_handler(lambda m: start_handler(m, pool), commands={"start"})
    dp.register_message_handler(lambda m: health_handler(m, provider, pool), commands={"health"})
    dp.register_message_handler(lambda m: pay_handler(m, bot, pool), commands={"pay"})
    dp.register_message_handler(lambda m: verify_handler(m, bot, provider, pool), commands={"verify"})
    dp.register_message_handler(lambda m: payments_handler(m, pool), commands={"payments"})
    dp.register_message_handler(lambda m: debug_tx_handler(m, provider, pool), commands={"debug_tx"})
    dp.register_message_handler(lambda m: debug_addr_handler(m, provider, pool), commands={"debug_addr"})
    dp.register_message_handler(lambda m: set_wallet_handler(m, pool), commands={"set_wallet"})
    dp.register_message_handler(lambda m: profile_handler(m, pool), lambda m: m.text == "Мой профиль")

    # сканер
    dp.register_message_handler(lambda m: scanner_on_handler(m, pool), commands={"scanner_on"})
    dp.register_message_handler(lambda m: scanner_off_handler(m, pool), commands={"scanner_off"})
    dp.register_message_handler(lambda m: scanner_settings_handler(m, pool), commands={"scanner_settings"})
    dp.register_message_handler(lambda m: set_discount_handler(m, pool), commands={"set_discount"})
    dp.register_message_handler(lambda m: set_maxprice_handler(m, pool), commands={"set_maxprice"})
    dp.register_message_handler(lambda m: set_collections_handler(m, pool), commands={"set_collections"})
    dp.register_message_handler(lambda m, b=bot: scanner_test_handler(m, b, pool), commands={"scanner_test"})

    # нижние кнопки
    dp.register_message_handler(lambda m: scanner_on_handler(m, pool), lambda m: m.text == "Купить NFT")
    dp.register_message_handler(lambda m: scanner_settings_handler(m, pool), lambda m: м.text == "О коллекции")
