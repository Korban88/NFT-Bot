# handlers.py
import asyncio
import os
import uuid
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Dict, Any, List
from urllib.parse import quote
from decimal import Decimal

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
TON_DECIMALS = Decimal(10**9)

# Сканер
LISTINGS_FEED_URL = os.getenv("LISTINGS_FEED_URL", "").strip()
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "60"))
SCAN_LOOKBACK_MIN = int(os.getenv("SCAN_LOOKBACK_MIN", "180"))

# Админ для служебных команд
ADMIN_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "347552741"))

# ======== Клавиатура ========
def main_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("Купить NFT"))
    kb.add(KeyboardButton("О коллекции"))
    kb.add(KeyboardButton("Мой профиль"))
    return kb

# ======== Утилиты ========
def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)

# ======== TonAPI/TonCenter провайдеры транзакций (как было) ========
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

# ======== Платежи (ссылки) ========
def gen_comment() -> str:
    return "nftbot-" + uuid.uuid4().hex[:12]

def build_ton_transfer_link(address: str, amount_ton: Decimal, comment: str) -> str:
    nanotons = int(amount_ton * TON_DECIMALS)
    safe_comment = comment[:120]
    return f"ton://transfer/{address}?amount={nanotons}&text={safe_comment}"

def build_tonkeeper_link(address: str, amount_ton: Decimal, comment: str) -> str:
    nanotons = int(amount_ton * TON_DECIMALS)
    return f"https://app.tonkeeper.com/transfer/{address}?amount={nanotons}&text={quote(comment)}"

def build_tonhub_link(address: str, amount_ton: Decimal, comment: str) -> str:
    nanotons = int(amount_ton * TON_DECIMALS)
    return f"https://tonhub.com/transfer/{address}?amount={nanotons}&text={quote(comment)}"

MIN_PAYMENT_TON = Decimal(os.getenv("MIN_PAYMENT_TON", "0.1"))

# ======== Команды общего назначения ========
async def start_handler(m: types.Message, pool: asyncpg.Pool):
    await m.answer(
        "NFT Бот: сканер выгодных лотов и витрина коллекции.\n"
        "Команды:\n"
        "/pay — оплата доступа\n"
        "/scanner_on, /scanner_off, /scanner_settings, /scanner_reset\n"
        "/set_discount, /set_maxprice, /set_collections\n"
        "/scanner_test, /scanner_source, /scanner_ping",
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
    tail = (wa[-6:] if wa else "—")
    head = (wa[:6] if wa else "")
    await m.answer(f"Health:\nTonAPI: {'ok' if ok else 'fail'}\nWallet: {head}…{tail}")

async def pay_handler(m: types.Message, bot: Bot, pool: asyncpg.Pool):
    wallet = await get_wallet(pool)
    if not wallet:
        await m.answer("Адрес приёма не задан. Укажи его: /set_wallet <адрес TON>")
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

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Tonkeeper", url=tk_link),
        InlineKeyboardButton("Tonhub", url=th_link),
    )
    msg = (
        "Оплата доступа.\n\n"
        f"Сумма: {MIN_PAYMENT_TON} TON или больше\n"
        f"Комментарий: `{comment}`\n"
        f"Адрес: `{wallet}`\n"
        f"Ссылка: {ton_link}\n\n"
        "После оплаты вернись и нажми «Проверить».")
    await m.answer(msg, parse_mode="Markdown", reply_markup=kb)

# ======== Вспомогательное для лотов ========
def _item_discount(it: Dict[str, Any]) -> Optional[float]:
    try:
        p = Decimal(str(it.get("price_ton")))
        f = Decimal(str(it.get("floor_ton")))
        if p > 0 and f > 0:
            return float((f - p) / f * 100)
    except Exception:
        pass
    return None

def _deal_id(it: Dict[str, Any]) -> str:
    raw = f"{it.get('collection','')}|{it.get('name','')}|{it.get('url','')}|{it.get('price_ton','')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()

def _item_matches(it: Dict[str, Any], st: Dict[str, Any]) -> bool:
    # возраст
    ts = it.get("timestamp")
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
            await bot.send_photo(user_id, img, caption=caption, reply_markup=kb)
            return
        except Exception:
            pass
    await bot.send_message(user_id, caption, reply_markup=kb)

# ======== Быстрый скан по кнопке «Обновить» ========
async def quick_scan_for_user(bot: Bot, user_id: int, pool: asyncpg.Pool, max_items: int = 3) -> int:
    items = await fetch_listings()
    if not items:
        await bot.send_message(user_id, "Фид пуст или недоступен.")
        return 0

    st = await get_or_create_scanner_settings(pool, user_id)
    sent = 0
    for it in items:
        it = dict(it)
        it["discount"] = _item_discount(it)
        it["deal_id"] = _deal_id(it)

        if not _item_matches(it, st):
            continue
        if await was_deal_seen(pool, it["deal_id"]):
            continue

        await send_item_alert(bot, user_id, it)
        await mark_deal_seen(pool, it)
        sent += 1
        if sent >= max_items:
            break

    if sent == 0:
        await bot.send_message(user_id, "Сейчас подходящих лотов нет.")
    return sent

# ======== Сканер: загрузка фида ========
async def fetch_listings() -> List[Dict[str, Any]]:
    """Элемент:
    {"name":"...","collection":"FLIGHT","price_ton":8.5,"floor_ton":12.0,"timestamp":1723380000,"url":"https://...","image":"https://..."}
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
    await asyncio.sleep(3)
    while True:
        try:
            items = await fetch_listings()
            if not items:
                await asyncio.sleep(SCAN_INTERVAL_SEC); continue

            users = await get_scanner_users(pool)
            if not users:
                await asyncio.sleep(SCAN_INTERVAL_SEC); continue

            for it in items:
                it = dict(it)
                it["discount"] = _item_discount(it)
                it["deal_id"] = _deal_id(it)

                if await was_deal_seen(pool, it["deal_id"]):
                    continue

                for uid in users:
                    st = await get_or_create_scanner_settings(pool, uid)
                    if _item_matches(it, st):
                        await send_item_alert(bot, uid, it)

                await mark_deal_seen(pool, it)

            await asyncio.sleep(SCAN_INTERVAL_SEC)
        except Exception:
            await asyncio.sleep(SCAN_INTERVAL_SEC)

# ======== Кнопочное меню настроек ========
def _settings_text(st: Dict[str, Any]) -> str:
    min_disc = float(st["min_discount"])
    max_price = st["max_price_ton"]
    cols = st["collections"]
    max_price_text = "нет" if max_price is None else f"{float(max_price):.3f} TON"
    cols_text = "любой" if not cols else ", ".join(cols)
    return (
        "Текущие фильтры сканера:\n"
        f"— Мин. скидка: {min_disc:.1f}%\n"
        f"— Макс. цена: {max_price_text}\n"
        f"— Коллекции: {cols_text}\n\n"
        "Измени кнопками ниже:"
    )

def _settings_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("−5% скидки", callback_data="cfg:disc:-5"),
        InlineKeyboardButton("+5% скидки", callback_data="cfg:disc:+5"),
    )
    kb.add(
        InlineKeyboardButton("Цена ≤ 10 TON", callback_data="cfg:max:10"),
        InlineKeyboardButton("Снять лимит цены", callback_data="cfg:max:none"),
    )
    kb.add(
        InlineKeyboardButton("Коллекции: FLIGHT", callback_data="cfg:cols:FLIGHT"),
        InlineKeyboardButton("Коллекции: любой", callback_data="cfg:cols:none"),
    )
    kb.add(InlineKeyboardButton("Обновить", callback_data="cfg:refresh"))
    return kb

async def scanner_settings_handler(m: types.Message, pool: asyncpg.Pool):
    st = await get_or_create_scanner_settings(pool, m.from_user.id)
    await m.answer(_settings_text(st), reply_markup=_settings_kb())

async def _apply_cfg_action(user_id: int, action: str):
    # action: "disc:+5" | "disc:-5" | "max:10" | "max:none" | "cols:FLIGHT" | "cols:none" | "refresh"
    pool = await get_pool()
    key, _, val = action.partition(":")
    if key == "disc":
        st = await get_or_create_scanner_settings(pool, user_id)
        cur = float(st["min_discount"] or 0.0)
        delta = 5.0 if val == "+5" else -5.0
        newv = max(0.0, min(90.0, cur + delta))
        await update_scanner_settings(pool, user_id, min_discount=newv)
    elif key == "max":
        if val == "none":
            await update_scanner_settings(pool, user_id, max_price_ton=None)
        else:
            try:
                await update_scanner_settings(pool, user_id, max_price_ton=float(val))
            except Exception:
                pass
    elif key == "cols":
        if val == "none":
            await update_scanner_settings(pool, user_id, collections=None)
        else:
            await update_scanner_settings(pool, user_id, collections=[val])
    # refresh — ничего не меняем

async def cb_settings(call: types.CallbackQuery):
    try:
        _, action = call.data.split("cfg:", 1)
    except Exception:
        await call.answer("Некорректная команда")
        return

    await _apply_cfg_action(call.from_user.id, action)
    pool = await get_pool()
    st = await get_or_create_scanner_settings(pool, call.from_user.id)

    # Обновим текст настроек
    try:
        await call.message.edit_text(_settings_text(st), reply_markup=_settings_kb())
    except Exception:
        # если сообщение нельзя редактировать — отправим новое
        await call.message.answer(_settings_text(st), reply_markup=_settings_kb())

    # Если нажали "Обновить" — быстрый скан (до 3 новых лотов)
    if action == "refresh":
        bot = call.message.bot
        await quick_scan_for_user(bot, call.from_user.id, pool, max_items=3)

    await call.answer("Готово")

# ======== Команды сканера ========
async def scanner_on_handler(m: types.Message, pool: asyncpg.Pool):
    await set_scanner_enabled(pool, m.from_user.id, True)
    await m.answer("Мониторинг включён. Уведомлю о выгодных лотах.", reply_markup=main_kb())

async def scanner_off_handler(m: types.Message, pool: asyncpg.Pool):
    await set_scanner_enabled(pool, m.from_user.id, False)
    await m.answer("Мониторинг выключен.", reply_markup=main_kb())

async def set_discount_handler(m: types.Message, pool: asyncpg.Pool):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Укажи значение: /set_discount 30")
        return
    try:
        val = float(parts[1].replace(",", "."))
    except Exception:
        await m.answer("Некорректно. Пример: /set_discount 30")
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
        await m.answer("Некорректно. Пример: /set_maxprice 12.5")
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
        await m.answer("Сейчас подходящих лотов нет.")
    else:
        await m.answer(f"Отправлено лотов: {sent}")

# ======== Диагностика фида ========
async def scanner_source_handler(m: types.Message):
    url = LISTINGS_FEED_URL or "— не задан —"
    await m.answer(
        "Источник фида:\n"
        f"{url}\n\n"
        f"Интервал скана: {SCAN_INTERVAL_SEC} сек\n"
        f"Окно свежести: {SCAN_LOOKBACK_MIN} мин"
    )

async def scanner_ping_handler(m: types.Message):
    url = LISTINGS_FEED_URL
    if not url:
        await m.answer("LISTINGS_FEED_URL не задан.")
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url)
        if r.status_code != 200:
            await m.answer(f"Ошибка загрузки фида: HTTP {r.status_code}")
            return
        data = r.json()
        items = data if isinstance(data, list) else (data.get("items") or [])
        n = len(items) if isinstance(items, list) else 0
        names = []
        if isinstance(items, list):
            for it in items[:3]:
                nm = (it.get("name") or it.get("title") or "—")
                names.append(str(nm))
        names_txt = ", ".join(names) if names else "—"
        await m.answer(f"Фид OK. Элементов: {n}. Первые: {names_txt}")
    except Exception as e:
        await m.answer(f"Ошибка: не удалось прочитать фид ({type(e).__name__})")

# ======== Служебная: сброс антидубликатов (админ) ========
async def scanner_reset_handler(m: types.Message, pool: asyncpg.Pool):
    if m.from_user.id != ADMIN_ID:
        await m.answer("Команда доступна только администратору.")
        return
    async with (await get_pool()).acquire() as con:
        cnt = await con.fetchval("SELECT COUNT(*) FROM app_found_deals")
        await con.execute("TRUNCATE app_found_deals")
    await m.answer(f"Сброшено записей: {cnt}. Антидубликаты очищены.")

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
    dp.register_message_handler(lambda m: scanner_reset_handler(m, pool), commands={"scanner_reset"})
    dp.register_message_handler(scanner_source_handler, commands={"scanner_source"})
    dp.register_message_handler(scanner_ping_handler, commands={"scanner_ping"})

    # reply-кнопки (пример)
    dp.register_message_handler(lambda m: scanner_on_handler(m, pool), lambda m: m.text == "Купить NFT")
    dp.register_message_handler(lambda m: scanner_settings_handler(m, pool), lambda m: m.text == "О коллекции")

    # callbacks настроек
    dp.register_callback_query_handler(cb_settings, lambda c: c.data and c.data.startswith("cfg:"))
