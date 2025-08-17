# handlers.py
import asyncio
import os
import uuid
import hashlib
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

from config import settings
from db import (
    get_pool, set_wallet, get_wallet,
    get_or_create_scanner_settings, update_scanner_settings,
    set_scanner_enabled, get_scanner_users,
    was_deal_seen, mark_deal_seen
)

# ======== Конфиг ========
PAYMENT_TTL_MIN = int(os.getenv("PAYMENT_TTL_MIN", "30"))
TON_DECIMALS = Decimal(10**9)

SOURCE_DRIVER = os.getenv("SOURCE_DRIVER", "json").strip().lower()  # json | tonapi
LISTINGS_FEED_URL = os.getenv("LISTINGS_FEED_URL", "").strip()
TON_COLLECTIONS = [c.strip() for c in os.getenv("TON_COLLECTIONS", "").split(",") if c.strip()]

SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "60"))
SCAN_LOOKBACK_MIN = int(os.getenv("SCAN_LOOKBACK_MIN", "1440"))  # 24h
SCAN_PUSH_LIMIT = int(os.getenv("SCAN_PUSH_LIMIT", "5"))
SCAN_COLD_START_SKIP_SEND = (os.getenv("SCAN_COLD_START_SKIP_SEND", "true").lower() == "true")

# новая настройка метрики «скидки»: median или p75 (процентиль 75)
DISCOUNT_BASELINE = os.getenv("DISCOUNT_BASELINE", "median").strip().lower()

ADMIN_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "347552741"))

# внутренний флаг «первый проход»
_COLD_START = True

# ======== Клавиатура ========
def main_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("Купить NFT"))
    kb.add(KeyboardButton("О коллекции"))
    kb.add(KeyboardButton("Мой профиль"))
    return kb

def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)

# ======== Платежи ========
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

# ======== Общие команды ========
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

# ======== Лоты: расчёт и фильтры ========
def _safe_decimal(x) -> Optional[Decimal]:
    try:
        if x is None:
            return None
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None

def _to_ton(value: Optional[Decimal]) -> Optional[float]:
    if value is None:
        return None
    try:
        v = Decimal(value)
        # если очень большое — это нанотоны
        if v > 1_000_000:
            return float(v / TON_DECIMALS)
        return float(v)
    except Exception:
        return None

def _pct(a: float, b: float) -> Optional[float]:
    if b and b > 0:
        return float((b - a) / b * 100.0)
    return None

def _median(values: List[float]) -> float:
    n = len(values)
    if n == 0:
        return 0.0
    arr = sorted(values)
    mid = n // 2
    if n % 2:
        return arr[mid]
    return (arr[mid - 1] + arr[mid]) / 2.0

def _p75(values: List[float]) -> float:
    if not values:
        return 0.0
    arr = sorted(values)
    idx = int(0.75 * (len(arr) - 1))
    return arr[idx]

def _deal_id(it: Dict[str, Any]) -> str:
    raw = f"{it.get('collection','')}|{it.get('nft_address','')}|{it.get('price_ton','')}"
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

    # скидка: если min_discount==0 — не фильтруем
    try:
        min_disc = float(st.get("min_discount") or 0.0)
    except Exception:
        min_disc = 0.0
    disc = it.get("discount")
    if min_disc > 0.0:
        if disc is None or float(disc) < min_disc:
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

    # коллекции (если задан список адресов)
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
    median = it.get("median_ton")
    disc = it.get("discount")  # к медиане
    lines = [f"🔥 {name}", f"Коллекция: {coll}"]
    if price is not None:
        try:
            lines.append(f"Цена: {float(price):.3f} TON")
        except Exception:
            lines.append(f"Цена: {price} TON")
    if floor is not None:
        try:
            lines.append(f"Floor: {float(floor):.3f} TON")
        except Exception:
            lines.append(f"Floor: {floor} TON")
    if median is not None:
        try:
            lines.append(f"Медиана: {float(median):.3f} TON")
        except Exception:
            pass
    if disc is not None:
        lines.append(f"Скидка к медиане: {float(disc):.1f}%")
    return "\n".join(lines)

async def send_item_alert(bot: Bot, user_id: int, it: Dict[str, Any]):
    kb = InlineKeyboardMarkup(row_width=2)
    if it.get("url"):
        kb.insert(InlineKeyboardButton("Tonviewer", url=it["url"]))
    if it.get("gg_url"):
        kb.insert(InlineKeyboardButton("Getgems", url=it["gg_url"]))
    caption = _item_caption(it)
    img = (it.get("image") or "").strip()
    if img:
        try:
            await bot.send_photo(user_id, img, caption=caption, reply_markup=kb)
            return
        except Exception:
            pass
    await bot.send_message(user_id, caption, reply_markup=kb)

# ======== Источники ========
def _tonviewer_url(addr: str) -> str:
    return f"https://tonviewer.com/{addr}"

def _getgems_url(addr: str) -> str:
    return f"https://getgems.io/nft/{addr}"

def _parse_iso_ts(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
        return int(datetime.fromisoformat(s2).timestamp())
    except Exception:
        return None

def _extract_sale_price_ton(sale: Dict[str, Any]) -> Optional[float]:
    """
    Нормализуем цену из TonAPI:
    - может быть числом/строкой в нанотонах
    - либо объект: {"value":"..."}, {"amount":{"value":"..."}}, etc.
    """
    if sale is None:
        return None
    cand = None

    for key in ("price", "full_price", "amount"):
        val = sale.get(key)
        if isinstance(val, (int, float, str, Decimal)):
            cand = _safe_decimal(val)
            if cand is not None:
                break
        if isinstance(val, dict):
            inner = val.get("value") or val.get("amount") or val.get("nano")
            cand = _safe_decimal(inner)
            if cand is not None:
                break

    if cand is None:
        cand = _safe_decimal(sale.get("value"))

    return _to_ton(cand)

async def _fetch_from_json_feed() -> List[Dict[str, Any]]:
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

async def _fetch_collection_items(client: httpx.AsyncClient, coll_addr: str) -> List[Dict[str, Any]]:
    """Подгружаем предметы коллекции, возвращаем только те, что в продаже; 1 ретрай при 429."""
    url = f"https://tonapi.io/v2/nfts/collections/{coll_addr}/items?limit=50&offset=0"
    for attempt in (0, 1):
        r = await client.get(url)
        if r.status_code == 200:
            data = r.json() or {}
            return data.get("nft_items") or data.get("items") or []
        if r.status_code == 429 and attempt == 0:
            await asyncio.sleep(0.9)
            continue
        return []
    return []

async def _fetch_from_tonapi() -> List[Dict[str, Any]]:
    """
    Берём ТОЛЬКО NFT, которые в продаже (есть sale).
    Линки: Tonviewer (всегда), + кнопка Getgems если маркетплейс = getgems.
    Считаем по коллекциям: floor и baseline (медиана/п75), скидка = (baseline - price)/baseline * 100.
    """
    if not TON_COLLECTIONS:
        return []
    headers = {}
    if getattr(settings, "TONAPI_KEY", None):
        headers["Authorization"] = f"Bearer {settings.TONAPI_KEY}"

    rows: List[Dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            total_cap = 200  # общий колпак
            for coll_addr in TON_COLLECTIONS:
                if total_cap <= 0:
                    break
                items = await _fetch_collection_items(client, coll_addr)
                for it in items:
                    sale = it.get("sale") or it.get("marketplace")
                    if not isinstance(sale, dict):
                        continue  # только выставленные

                    addr = it.get("address") or ""
                    if not addr:
                        continue

                    price_ton = _extract_sale_price_ton(sale)

                    img = None
                    previews = it.get("previews") or []
                    if isinstance(previews, list) and previews:
                        img = previews[-1].get("url") or previews[0].get("url")

                    name = (it.get("metadata") or {}).get("name") or addr

                    ts = _parse_iso_ts(sale.get("created_at")) or _parse_iso_ts(it.get("created_at")) \
                         or int(_now_utc().timestamp())

                    url_view = _tonviewer_url(addr)
                    gg_url = None
                    market_name = (sale.get("marketplace") or {}).get("name") if isinstance(sale.get("marketplace"), dict) else sale.get("market") or sale.get("name")
                    if isinstance(market_name, str) and "getgems" in market_name.lower():
                        gg_url = _getgems_url(addr)

                    rows.append({
                        "name": name,
                        "collection": coll_addr,
                        "nft_address": addr,
                        "price_ton": price_ton,
                        "floor_ton": None,    # заполним ниже
                        "median_ton": None,   # заполним ниже
                        "discount": None,     # заполним ниже (к медиане/п75)
                        "timestamp": ts,
                        "url": url_view,
                        "gg_url": gg_url,
                        "image": img,
                    })
                    total_cap -= 1
                    if total_cap <= 0:
                        break
    except Exception:
        return []

    # Сводные цены по коллекциям
    by_coll: Dict[str, List[float]] = {}
    for x in rows:
        p = _safe_decimal(x.get("price_ton"))
        if p and p > 0:
            by_coll.setdefault(x["collection"], []).append(float(p))

    floors: Dict[str, float] = {c: min(v) for c, v in by_coll.items() if v}
    baselines: Dict[str, float] = {}
    for c, prices in by_coll.items():
        if not prices:
            continue
        if DISCOUNT_BASELINE == "p75":
            baselines[c] = _p75(prices)
        else:
            baselines[c] = _median(prices)

    # Проставляем floor / baseline / discount
    for x in rows:
        coll = x["collection"]
        floor = floors.get(coll)
        base = baselines.get(coll)
        if floor:
            x["floor_ton"] = floor
        if base:
            x["median_ton"] = base
        price = x.get("price_ton")
        if price and base and base > 0:
            try:
                x["discount"] = _pct(float(price), float(base))
            except Exception:
                pass

    return rows

async def fetch_listings() -> List[Dict[str, Any]]:
    if SOURCE_DRIVER == "tonapi":
        return await _fetch_from_tonapi()
    return await _fetch_from_json_feed()

# ======== Отбор и сортировка ========
def _rank_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # по убыванию скидки (к медиане), потом по цене
    def key(it):
        disc = it.get("discount")
        disc_key = -9999.0 if disc is None else float(disc)
        price = it.get("price_ton")
        price_key = 9999999.0 if price is None else float(price)
        return (-disc_key, price_key)
    return sorted(items, key=key)

# ======== Быстрый скан ========
async def quick_scan_for_user(bot: Bot, user_id: int, pool: asyncpg.Pool, max_items: int = 3) -> int:
    items = await fetch_listings()
    if not items:
        await bot.send_message(user_id, "Фид пуст или недоступен.")
        return 0

    st = await get_or_create_scanner_settings(pool, user_id)
    cand = [it for it in items if _item_matches(it, st)]
    cand = _rank_items(cand)

    sent = 0
    for it in cand:
        it = dict(it)
        it["deal_id"] = _deal_id(it)
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

# ======== Фоновая задача ========
async def scanner_loop(bot: Bot, pool: asyncpg.Pool):
    global _COLD_START
    await asyncio.sleep(3)
    while True:
        try:
            items = await fetch_listings()
            if not items:
                await asyncio.sleep(SCAN_INTERVAL_SEC); continue

            users = await get_scanner_users(pool)
            if not users:
                await asyncio.sleep(SCAN_INTERVAL_SEC); continue

            for uid in users:
                st = await get_or_create_scanner_settings(pool, uid)
                cand = [it for it in items if _item_matches(it, st)]
                cand = _rank_items(cand)

                pushed = 0
                for it in cand:
                    it = dict(it)
                    it["deal_id"] = _deal_id(it)
                    if await was_deal_seen(pool, it["deal_id"]):
                        continue

                    if _COLD_START and SCAN_COLD_START_SKIP_SEND:
                        await mark_deal_seen(pool, it)
                        continue

                    await send_item_alert(bot, uid, it)
                    await mark_deal_seen(pool, it)
                    pushed += 1
                    if pushed >= SCAN_PUSH_LIMIT:
                        break

            _COLD_START = False
            await asyncio.sleep(SCAN_INTERVAL_SEC)
        except Exception:
            await asyncio.sleep(SCAN_INTERVAL_SEC)

# ======== Настройки ========
def _settings_text(st: Dict[str, Any]) -> str:
    min_disc = float(st["min_discount"])
    max_price = st["max_price_ton"]
    cols = st["collections"]
    max_price_text = "нет" if max_price is None else f"{float(max_price):.3f} TON"
    cols_text = "любой" if not cols else ", ".join(cols)
    src = "TonAPI (в продаже, tonviewer)" if SOURCE_DRIVER == "tonapi" else (LISTINGS_FEED_URL or "JSON-фид не задан")
    return (
        f"Источник: {src}\n"
        f"Метрика скидки: {'медиана' if DISCOUNT_BASELINE == 'median' else 'p75'} (по коллекции)\n"
        f"Лимит рассылки за цикл: {SCAN_PUSH_LIMIT}\n"
        f"Холодный старт: {'skip' if SCAN_COLD_START_SKIP_SEND else 'send'}\n\n"
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

async def cb_settings(call: types.CallbackQuery):
    try:
        _, action = call.data.split("cfg:", 1)
    except Exception:
        await call.answer("Некорректная команда")
        return

    await _apply_cfg_action(call.from_user.id, action)
    pool = await get_pool()
    st = await get_or_create_scanner_settings(pool, call.from_user.id)

    try:
        await call.message.edit_text(_settings_text(st), reply_markup=_settings_kb())
    except Exception:
        await call.message.answer(_settings_text(st), reply_markup=_settings_kb())

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
    await m.answer("Мониторинг выключен.", reply_markup=main_kб())

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
    """Предпросмотр: ТОП-5 подходящих лотов, БЕЗ антидубликатов."""
    items = await fetch_listings()
    if not items:
        await m.answer("Фид пуст или недоступен (настрой источник).")
        return
    st = await get_or_create_scanner_settings(pool, m.from_user.id)
    cand = _rank_items([it for it in items if _item_matches(it, st)])

    sent = 0
    for it in cand[:5]:
        await send_item_alert(bot, m.from_user.id, it)
        sent += 1
    if sent == 0:
        await m.answer("Сейчас подходящих лотов нет.")
    else:
        await m.answer(f"Отправлено лотов: {sent}")

# ======== Диагностика ========
async def scanner_source_handler(m: types.Message):
    if SOURCE_DRIVER == "tonapi":
        src = f"TonAPI (в продаже, tonviewer) / коллекций: {len(TON_COLLECTIONS)} / лимит: {SCAN_PUSH_LIMIT}"
    else:
        src = LISTINGS_FEED_URL or "— не задан —"
    await m.answer(
        "Источник фида:\n"
        f"{src}\n\n"
        f"Метрика скидки: {'медиана' if DISCOUNT_BASELINE == 'median' else 'p75'} (по коллекции)\n"
        f"Интервал скана: {SCAN_INTERVAL_SEC} сек\n"
        f"Окно свежести: {SCAN_LOOKBACK_MIN} мин\n"
        f"Холодный старт: {'skip' if SCAN_COLD_START_SKIP_SEND else 'send'}"
    )

async def scanner_ping_handler(m: types.Message):
    try:
        if SOURCE_DRIVER == "tonapi":
            items = await _fetch_from_tonapi()
        else:
            items = await _fetch_from_json_feed()
        n = len(items)
        names = [str((it.get("name") or "—")) for it in items[:3]]
        await m.answer(f"Источник OK. Элементов (в продаже): {n}. Первые: {', '.join(names) if names else '—'}")
    except Exception as e:
        await m.answer(f"Ошибка пинга источника: {type(e).__name__}")

# ======== Сброс антидубликатов (админ) ========
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

    # reply-кнопки
    dp.register_message_handler(lambda m: scanner_on_handler(m, pool), lambda m: m.text == "Купить NFT")
    dp.register_message_handler(lambda m: scanner_settings_handler(m, pool), lambda m: m.text == "О коллекции")

    dp.register_callback_query_handler(cb_settings, lambda c: c.data and c.data.startswith("cfg:"))
