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
import hashlib

import httpx
import asyncpg
from aiogram import Dispatcher, types, Bot
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.dispatcher.filters import Command

from config import settings
from db import (
    get_pool, set_wallet, get_wallet,
    get_or_create_scanner_settings, update_scanner_settings,
    set_scanner_enabled, get_scanner_users,
    was_deal_seen, mark_deal_seen
)

# ======== Конфиг ========
PAYMENT_TTL_MIN = int(os.getenv("PAYMENT_TTL_MIN", "30"))
LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "Europe/Moscow")
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
TON_DECIMALS = Decimal(10**9)

# Сканер
LISTINGS_FEED_URL = os.getenv("LISTINGS_FEED_URL", "").strip()
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "60"))
SCAN_LOOKBACK_MIN = int(os.getenv("SCAN_LOOKBACK_MIN", "180"))

# ======== Клавиатура ========
def main_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("Купить NFT"))
    kb.add(KeyboardButton("О коллекции"))
    kb.add(KeyboardButton("Мой профиль"))
    return kb

# ======== Утилиты времени/форматирования ========
def _fmt_local(dt_utc: datetime) -> str:
    return dt_utc.astimezone(LOCAL_TZ).strftime("%d.%m %H:%M")

def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)

# ======== TonAPI/TonCenter провайдеры транзакций (как у тебя было) ========
class TonAPIProvider:
    def __init__(self, api_key: str):
        self.base = "https://tonapi.io/v2/blockchain"
        self.api_key = api_key

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def convert(self, addr: str) -> Dict[str, str]:
        try:
            url = f"{self.base}/accounts/{addr}/parse"
            async with httpx.AsyncClient(timeout=12.0) as client:
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
        url = f"{self.base}/accounts/{account_id}/transactions?limit={min(limit, 100)}"
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
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params=params)
        if r.status_code == 200:
            data = r.json() or {}
            return r.status_code, data.get("result")
        return r.status_code, None

def gen_comment() -> str:
    return "nftbot-" + uuid.uuid4().hex[:12]

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

# ======== Команды общего назначения ========
async def start_handler(m: types.Message, pool: asyncpg.Pool):
    await m.answer(
        "NFT Бот: сканер выгодных лотов и витрина коллекции.\n"
        "Команды:\n"
        "/pay — оплата доступа\n"
        "/scanner_on, /scanner_off, /scanner_settings\n"
        "/set_discount, /set_maxprice, /set_collections\n"
        "/debug_addr, /debug_tx, /scanner_test",
        reply_markup=main_kb()
    )

async def set_wallet_handler(m: types.Message, pool: asyncpg.Pool):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Укажи адрес: /set_wallet EQ... или UQ...")
        return
    addr = parts[1].strip()
    await set_wallet(pool, addr)
    await m.answer("Адрес приёма обновлён.")

async def health_handler(m: types.Message, pool: asyncpg.Pool):
    ok = bool(settings.TONAPI_KEY)
    wa = await get_wallet(pool)
    await m.answer(
        "Health:\n"
        f"TonAPI: {'ok' if ok else 'fail'}\n"
        f"Wallet: {wa[:6]}…{wa[-6:] if wa else '—'}"
    )

# ======== Оплата (кратко, как было) ========
MIN_PAYMENT_TON = Decimal(os.getenv("MIN_PAYMENT_TON", "0.1"))

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
        f"Ссылка: {ton_link}\n\n"
        f"Оплатите и вернитесь сюда — проверка займёт до {ttl_text}."
    )
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Tonkeeper", url=tk_link),
        InlineKeyboardButton("Tonhub", url=th_link),
    )
    await m.answer(msg, parse_mode="Markdown", reply_markup=kb)

# ======== DEBUG (кошелёк/транзакции) — уже были у тебя, оставил кратко ========
def _extract_comment_from_msg(msg: Dict[str, Any]) -> Optional[str]:
    for k in ("message", "comment", "decoded_body", "payload"):
        v = msg.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict) and isinstance(v.get("comment"), str):
            return v["comment"]
    return None

def _extract_amount_from_msg(msg: Dict[str, Any]) -> Optional[float]:
    for k in ("value", "amount", "nanoton", "nanograms"):
        v = msg.get(k)
        if v is None:
            continue
        try:
            nano = int(v)
            return float(Decimal(nano) / TON_DECIMALS)
        except Exception:
            pass
    return None

def _tx_id_str(tx: Dict[str, Any]) -> str:
    return str(tx.get("hash") or tx.get("transaction_id") or tx.get("id") or "tx")

def _fmt_msgs_list(tx: Dict[str, Any]) -> List[Dict[str, Any]]:
    msgs: List[Dict[str, Any]] = []
    for key in ["in_msg", "in_msg_desc"]:
        if isinstance(tx.get(key), dict):
            msgs.append(tx[key])
    for key in ["out_msgs"]:
        if isinstance(tx.get(key), list):
            msgs.extend([m for m in tx[key] if isinstance(m, dict)])
    if isinstance(tx.get("messages"), list):
        msgs.extend([m for m in tx["messages"] if isinstance(m, dict)])
    return msgs

async def debug_addr_handler(m: types.Message, pool: asyncpg.Pool):
    wallet = await get_wallet(pool)
    if not wallet:
        await m.answer("Адрес приёма не задан. Укажи его: /set_wallet <адрес TON>")
        return
    lines: List[str] = [f"Адрес: {wallet}"]
    await m.answer("\n".join(lines))

# ======== Сканер: формат карточки и фильтры ========
def _item_discount(it: Dict[str, Any]) -> Optional[float]:
    try:
        p = Decimal(str(it.get("price_ton")))
        f = Decimal(str(it.get("floor_ton")))
        if p > 0 and f > 0:
            d = (f - p) / f * 100
            return float(d)
    except Exception:
        return None
    return None

def _deal_id(it: Dict[str, Any]) -> str:
    raw = f"{it.get('collection','')}|{it.get('name','')}|{it.get('url','')}|{it.get('price_ton','')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()

def _item_matches(it: Dict[str, Any], st: Dict[str, Any]) -> bool:
    # возраст
    ts = it.get("timestamp")  # seconds
    if ts:
        try:
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            if dt < _now_utc() - timedelta(minutes=SCAN_LOOKBACK_MIN):
                return False
        except Exception:
            pass
    # скидка
    disc = _item_discount(it)
    if disc is not None and float(disc) < float(st.get("min_discount") or 0.0):
        return False
    # цена
    maxp = st.get("max_price_ton")
    if maxp is not None:
        try:
            price = float(it.get("price_ton") or 0)
            if price <= 0 or price > float(maxp):
                return False
        except Exception:
            return False
    # коллекции
    cols = st.get("collections") or []
    if cols:
        col = (it.get("collection") or "").strip()
        # допускаем совпадение по алиасу
        return any(col.lower() == c.lower().strip() for c in cols)
    return True

def _item_caption(it: Dict[str, Any]) -> str:
    name = it.get("name") or "—"
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

async def send_item_alert(bot: Bot, user_id: int, it: Dict[str, Any]):
    kb = InlineKeyboardMarkup()
    if it.get("url"):
        kb.add(InlineKeyboardButton("Открыть лот", url=it["url"]))
    caption = _item_caption(it)
    img = (it.get("image") or "").strip()
    if img:
        try:
            await bot.send_photo(user_id, img, caption=caption)
            return
        except Exception:
            pass
    await bot.send_message(user_id, caption, reply_markup=kb)

# ======== Сканер: загрузка фида ========
async def fetch_listings() -> List[Dict[str, Any]]:
    """Ожидаемый формат элемента:
    {
      "name": "...", "collection": "FLIGHT", "price_ton": 12.3, "floor_ton": 16.0,
      "timestamp": 1723350000, "url": "https://...", "image": "https://..."
    }
    """
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
            if not items:
                await asyncio.sleep(SCAN_INTERVAL_SEC)
                continue

            users = await get_scanner_users(pool)
            if not users:
                await asyncio.sleep(SCAN_INTERVAL_SEC)
                continue

            # для каждого подходящего лота — антидубликат и рассылка включённым юзерам
            for it in items:
                it = dict(it)
                it["discount"] = _item_discount(it)
                it["deal_id"] = _deal_id(it)

                if await was_deal_seen(pool, it["deal_id"]):
                    continue

                # фильтруем по каждому пользователю персонально
                for uid in users:
                    st = await get_or_create_scanner_settings(pool, uid)
                    if _item_matches(it, st):
                        await send_item_alert(bot, uid, it)

                await mark_deal_seen(pool, it)

            await asyncio.sleep(SCAN_INTERVAL_SEC)

        except Exception:
            await asyncio.sleep(SCAN_INTERVAL_SEC)

# ======== Handlers: сканер команды ========
async def scanner_on_handler(m: types.Message, pool: asyncpg.Pool):
    await set_scanner_enabled(pool, m.from_user.id, True)
    await m.answer("Мониторинг включён. Уведомлю о выгодных лотах.", reply_markup=main_kb())

async def scanner_off_handler(m: types.Message, pool: asyncpg.Pool):
    await set_scanner_enabled(pool, m.from_user.id, False)
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
        "/set_collections col1,col2",
    ]
    await m.answer("\n".join(lines), reply_markup=main_kb())

async def set_discount_handler(m: types.Message, pool: asyncpg.Pool):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Укажи значение скидки, например: /set_discount 30")
        return
    try:
        val = float(parts[1].replace(",", "."))
    except Exception:
        await m.answer("Некорректное значение. Пример: /set_discount 30")
        return
    await update_scanner_settings(pool, m.from_user.id, min_discount=val)
    await m.answer("Мин. скидка обновлена.")

async def set_maxprice_handler(m: types.Message, pool: asyncpg.Pool):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await update_scanner_settings(pool, m.from_user.id, max_price_ton=None)
        await m.answer("Лимит цены снят.")
        return
    try:
        val = float(parts[1].replace(",", "."))
    except Exception:
        await m.answer("Некорректная цена. Пример: /set_maxprice 12.5")
        return
    await update_scanner_settings(pool, m.from_user.id, max_price_ton=val)
    await m.answer("Макс. цена обновлена.")

async def set_collections_handler(m: types.Message, pool: asyncpg.Pool):
    parts = (m.text or "").split(maxsplit=1)
    cols: Optional[List[str]] = None
    if len(parts) >= 2:
        cols = [c.strip() for c in parts[1].split(",") if c.strip()]
    await update_scanner_settings(pool, m.from_user.id, collections=cols)
    await m.answer("Список коллекций обновлён.")

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

# ======== Регистрация ========
def register_handlers(dp: Dispatcher, bot: Bot, pool: asyncpg.Pool):
    dp.register_message_handler(lambda m: start_handler(m, pool), commands={"start"})
    dp.register_message_handler(lambda m: set_wallet_handler(m, pool), commands={"set_wallet"})
    dp.register_message_handler(lambda m: health_handler(m, pool), commands={"health"})

    dp.register_message_handler(lambda m, b=bot: pay_handler(m, b, pool), commands={"pay"})

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
    dp.register_message_handler(lambda m: scanner_settings_handler(m, pool), lambda m: m.text == "О коллекции")
