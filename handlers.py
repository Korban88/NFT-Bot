# handlers.py
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Dict, Any, List
from urllib.parse import quote
from decimal import Decimal, InvalidOperation

import httpx
import asyncpg
from aiogram import Dispatcher, types, Bot
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)

# ======== Config ========
ENV_WALLET = os.getenv("TON_WALLET_ADDRESS", "").strip()
TONAPI_KEY = os.getenv("TONAPI_KEY", "").strip()
MIN_PAYMENT_TON = Decimal(os.getenv("MIN_PAYMENT_TON", "0.1"))  # TON
TON_DECIMALS = Decimal(10**9)

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

async def ensure_tables(pool: asyncpg.Pool):
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(CREATE_APP_PAYMENTS_SQL)
            await con.execute(CREATE_APP_USERS_SQL)
            await con.execute(CREATE_APP_CONFIG_SQL)

async def upsert_user(pool: asyncpg.Pool, user_id: int):
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO app_users (user_id) VALUES ($1) "
            "ON CONFLICT (user_id) DO UPDATE SET updated_at=now()",
            user_id,
        )

async def get_wallet(pool: asyncpg.Pool) -> str:
    """Адрес для приёма платежей: сначала из БД, затем из ENV."""
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

# ======== TonAPI ========
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

    async def _convert_address(self, address: str) -> Dict[str, str]:
        url = f"{self.base}/tools/convert_address?address={address}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, headers=self._headers())
            r.raise_for_status()
            data = r.json()
            return {
                "orig": address,
                "bounceable": (data.get("bounceable") or {}).get("b64url") or "",
                "non_bounceable": (data.get("non_bounceable") or {}).get("b64url") or "",
                "raw": data.get("raw") or "",
            }
        except Exception:
            return {"orig": address, "bounceable": "", "non_bounceable": "", "raw": ""}

    async def _fetch_tx(self, account_id: str, limit: int) -> Tuple[int, Optional[List[Dict[str, Any]]]]:
        url = f"{self.base}/accounts/{account_id}/transactions?limit={limit}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers=self._headers())
        if r.status_code == 200:
            data = r.json()
            return r.status_code, (data.get("transactions", []) or data.get("items", []) or [])
        return r.status_code, None

    async def list_recent(self, address: str, limit: int = 20) -> List[Dict[str, Any]]:
        # как есть
        _, items = await self._fetch_tx(address, limit)
        if items is not None:
            return items
        # нормализуем и ретраим
        forms = await self._convert_address(address)
        for key in ["bounceable", "non_bounceable", "raw"]:
            acc = forms.get(key)
            if acc:
                code, items = await self._fetch_tx(acc, limit)
                if items is not None:
                    return items
        return []

    @staticmethod
    def _to_ton(nanotons: Any) -> Optional[Decimal]:
        try:
            return Decimal(str(nanotons)) / TON_DECIMALS
        except (InvalidOperation, TypeError):
            return None

    @staticmethod
    def _extract_comment(msg: Dict[str, Any]) -> str:
        return str(
            msg.get("message")
            or msg.get("comment")
            or msg.get("decoded_body", {}).get("text")
            or msg.get("decoded", {}).get("body", {}).get("text")
            or ""
        ).strip()

    @staticmethod
    def _extract_amount_ton(msg: Dict[str, Any]) -> Optional[Decimal]:
        for candidate in [
            msg.get("value"),
            msg.get("amount"),
            msg.get("decoded_body", {}).get("amount"),
            msg.get("decoded", {}).get("body", {}).get("amount"),
        ]:
            ton = TonAPI._to_ton(candidate)
            if ton is not None:
                return ton
        return None

    async def find_incoming_with_comment(
        self, address: str, comment: str, min_amount_ton: Decimal, lookback_minutes: int = 360
    ) -> Optional[Tuple[str, Decimal]]:
        since_dt = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
        items = await self.list_recent(address, limit=100)
        if not items:
            return None

        wanted = comment.strip().lower()

        for tx in items:
            utime = tx.get("utime") or tx.get("timestamp")
            if utime:
                tx_dt = datetime.fromtimestamp(int(utime), tz=timezone.utc)
                if tx_dt < since_dt:
                    continue

            tx_hash = tx.get("hash") or tx.get("transaction_id") or tx.get("lt") or "unknown"

            msgs: List[Dict[str, Any]] = []
            if isinstance(tx.get("in_msg"), dict):
                msgs.append(tx["in_msg"])
            if isinstance(tx.get("out_msgs"), list):
                msgs.extend([m for m in tx["out_msgs"] if isinstance(m, dict)])
            if isinstance(tx.get("messages"), list):
                msgs.extend([m for m in tx["messages"] if isinstance(m, dict)])

            for msg in msgs:
                cmt = self._extract_comment(msg).lower()
                amt = self._extract_amount_ton(msg)
                if cmt == wanted and amt is not None and amt >= min_amount_ton:
                    return tx_hash, amt
        return None

# ======== Utils ========
def gen_comment() -> str:
    return f"pay-{uuid.uuid4().hex[:6]}"

# ======== Handlers ========
async def start_handler(m: types.Message, pool: asyncpg.Pool):
    await upsert_user(pool, m.from_user.id)
    wa = await get_wallet(pool)
    text = (
        "Добро пожаловать в NFT бот.\n\n"
        "Доступные команды:\n"
        "/scanner_on — включить мониторинг лотов\n"
        "/scanner_off — выключить мониторинг\n"
        "/scanner_settings — настройки фильтров\n"
        "/pay — ссылка на оплату (есть web‑кнопки)\n"
        "/verify pay-xxxxxx — проверить оплату по комментарию (подставь свой)\n"
        "/debug_tx — последние транзакции (отладка)\n"
        "/debug_addr — формы адреса и доступность TonAPI\n"
        "/set_wallet АДРЕС — сменить адрес приёма (только для тебя)\n"
        "/health — проверить TonAPI и Pinata\n\n"
        f"Текущий адрес приёма: {wa[:6]}…{wa[-6:] if wa else '—'}"
    )
    await m.answer(text, reply_markup=main_kb())

async def health_handler(m: types.Message, tonapi: TonAPI, pool: asyncpg.Pool):
    ok = await tonapi.health()
    wa = await get_wallet(pool)
    txt = (
        "Health:\n"
        f"TonAPI: {'ok' if ok else 'fail'}\n"
        f"Wallet: {wa[:6]}…{wa[-6:] if wa else '—'}"
    )
    await m.answer(txt)

async def pay_handler(m: types.Message, pool: asyncpg.Pool):
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

    msg = (
        "Оплата доступа/покупки.\n\n"
        f"Сумма: {MIN_PAYMENT_TON} TON или больше\n"
        f"Комментарий: `{comment}`\n"
        f"Адрес: `{wallet}`\n\n"
        "Если ссылка ниже не открывается на ПК — используй кнопки Tonkeeper/Tonhub (web) "
        "или оплати вручную из @wallet: вставь адрес и комментарий как указано выше.\n\n"
        f"После оплаты запусти: `/verify {comment}`"
    )
    await m.answer(msg, parse_mode="Markdown", reply_markup=pay_kb(ton_link, tk_link, th_link))

async def verify_handler(m: types.Message, tonapi: TonAPI, pool: asyncpg.Pool):
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
            "SELECT id, status FROM app_payments WHERE user_id=$1 AND comment=$2 ORDER BY created_at DESC LIMIT 1",
            m.from_user.id, comment
        )
    if not row:
        await m.answer("Платёж с таким комментарием у тебя не найден. Создай новый через /pay.")
        return
    if row["status"] == "paid":
        await m.answer("Этот платёж уже подтверждён.")
        return

    found = await tonapi.find_incoming_with_comment(wallet, comment, MIN_PAYMENT_TON, lookback_minutes=360)
    if not found:
        await m.answer("Платёж не найден. Проверь комментарий, сумму и адрес. Если платил только что — подожди 1–2 минуты и попробуй ещё раз.")
        return

    tx_hash, amount_ton = found
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE app_payments SET status='paid', tx_hash=$2, paid_at=now() WHERE id=$1",
            row["id"], tx_hash
        )
        await con.execute(
            "INSERT INTO app_users (user_id, scanner_enabled, updated_at) VALUES ($1, TRUE, now()) "
            "ON CONFLICT (user_id) DO UPDATE SET scanner_enabled=TRUE, updated_at=now()",
            m.from_user.id
        )

    await m.answer(
        "Оплата подтверждена.\n"
        f"Сумма: {amount_ton} TON\n"
        f"Tx: {tx_hash}\n\n"
        "Мониторинг лотов активирован. Команды: /scanner_settings, /scanner_off"
    )

async def debug_tx_handler(m: types.Message, tonapi: TonAPI, pool: asyncpg.Pool):
    wallet = await get_wallet(pool)
    if not wallet:
        await m.answer("Адрес приёма не задан. Укажи его: /set_wallet <адрес TON>")
        return
    items = await tonapi.list_recent(wallet, limit=10)
    if not items:
        await m.answer("Нет данных по транзакциям (TonAPI вернул пусто). Проверь адрес/задержку сети.")
        return

    lines = ["Последние транзакции:"]
    shown = 0
    for tx in items[:5]:
        tx_hash = tx.get("hash") or tx.get("transaction_id") or tx.get("lt") or "unknown"
        utime = tx.get("utime") or tx.get("timestamp")
        when = datetime.fromtimestamp(int(utime), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if utime else "—"
        msgs: List[Dict[str, Any]] = []
        if isinstance(tx.get("in_msg"), dict):
            msgs.append(tx["in_msg"])
        if isinstance(tx.get("out_msgs"), list):
            msgs.extend([m for m in tx["out_msgs"] if isinstance(m, dict)])
        if isinstance(tx.get("messages"), list):
            msgs.extend([m for m in tx["messages"] if isinstance(m, dict)])

        found_any = False
        for msg in msgs:
            cmt = TonAPI._extract_comment(msg)
            amt = TonAPI._extract_amount_ton(msg)
            if cmt or amt is not None:
                lines.append(f"• {when} | {tx_hash}\n  comment: {cmt or '—'}\n  amount: {amt if amt is not None else '—'} TON")
                found_any = True
                shown += 1
                if shown >= 5:
                    break
        if not found_any:
            lines.append(f"• {when} | {tx_hash}\n  (нет комм/суммы в доступных полях)")
        if shown >= 5:
            break

    await m.answer("\n".join(lines))

async def debug_addr_handler(m: types.Message, tonapi: TonAPI, pool: asyncpg.Pool):
    wallet = await get_wallet(pool)
    if not wallet:
        await m.answer("Адрес приёма не задан. Укажи его: /set_wallet <адрес TON>")
        return
    forms = await tonapi._convert_address(wallet)
    parts = [f"Исходный: {forms.get('orig') or '—'}",
             f"Bounceable: {forms.get('bounceable') or '—'}",
             f"Non-bounceable: {forms.get('non_bounceable') or '—'}",
             f"Raw: {forms.get('raw') or '—'}"]

    # Проверим, какие формы TonAPI принимает
    ok = []
    for key in ["orig", "bounceable", "non_bounceable", "raw"]:
        acc = forms.get(key)
        if not acc:
            continue
        code, items = await tonapi._fetch_tx(acc, limit=1)
        ok.append(f"{key}: HTTP {code}, items={'yes' if items else 'no'}")
    parts.append("\nПроверка эндпоинта:")
    parts.extend(ok or ["нет попыток"])

    await m.answer("\n".join(parts))

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
        txt.append(f"— {float(last_tx['amount_ton']):.3f} TON, {last_tx['paid_at'].strftime('%Y-%m-%d %H:%M:%S UTC')}")
    else:
        txt.append("— пока пусто")
    await m.answer("\n".join(txt))

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
    await m.answer("Мониторинг выключен.", reply_markup=main_kб())

async def scanner_settings_handler(m: types.Message, pool: asyncpg.Pool):
    await m.answer(
        "Настройки сканера (временно заглушка):\n"
        "— Скидка: ≥ 20–30%\n"
        "— Коллекции: выбранные\n"
        "— Цена/время/редкость: фильтры активны",
        reply_markup=main_kb()
    )

# ======== Router ========
def register_handlers(dp: Dispatcher, bot: Bot, pool: asyncpg.Pool):
    tonapi = TonAPI(TONAPI_KEY)
    loop = asyncio.get_event_loop()
    loop.create_task(ensure_tables(pool))

    dp.register_message_handler(lambda m: start_handler(m, pool), commands={"start"})
    dp.register_message_handler(lambda m: health_handler(m, tonapi, pool), commands={"health"})
    dp.register_message_handler(lambda m: pay_handler(m, pool), commands={"pay"})
    dp.register_message_handler(lambda m: verify_handler(m, tonapi, pool), commands={"verify"})
    dp.register_message_handler(lambda m: debug_tx_handler(m, tonapi, pool), commands={"debug_tx"})
    dp.register_message_handler(lambda m: debug_addr_handler(m, tonapi, pool), commands={"debug_addr"})
    dp.register_message_handler(lambda m: set_wallet_handler(m, pool), commands={"set_wallet"})
    dp.register_message_handler(lambda m: profile_handler(m, pool), lambda m: m.text == "Мой профиль")
    dp.register_message_handler(lambda m: scanner_on_handler(m, pool), commands={"scanner_on"})
    dp.register_message_handler(lambda m: scanner_off_handler(m, pool), commands={"scanner_off"})
    dp.register_message_handler(lambda m: scanner_settings_handler(m, pool), commands={"scanner_settings"})
    # Кнопки внизу
    dp.register_message_handler(lambda m: scanner_on_handler(m, pool), lambda m: m.text == "Купить NFT")
    dp.register_message_handler(lambda m: scanner_settings_handler(m, pool), lambda m: m.text == "О коллекции")
