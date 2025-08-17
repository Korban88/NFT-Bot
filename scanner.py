# scanner.py
import asyncio
import hashlib
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx
from aiogram import Bot

from config import settings
from db import (
    get_scanner_users,               # без аргументов
    get_or_create_scanner_settings,  # (user_id)
    was_deal_seen,                   # (deal_id)
    mark_deal_seen,                  # (deal_dict)
)

logger = logging.getLogger("nftbot.scanner")

# === Настройки источников ===
DEFAULT_TICK_SECONDS = int(os.getenv("SCANNER_TICK_SECONDS", "30"))

# TonAPI (агрегатор маркетов TON). Нужен токен.
TONAPI_BASE = os.getenv("TONAPI_BASE", "https://tonapi.io")
TONAPI_TOKEN = getattr(settings, "TONAPI_TOKEN", None) or os.getenv("TONAPI_TOKEN")

# Getgems GraphQL (публичный, но схема может меняться — обрабатываем мягко).
GETGEMS_ENABLED = os.getenv("GETGEMS_ENABLED", "1") == "1"
GETGEMS_GRAPHQL = os.getenv("GETGEMS_GRAPHQL", "https://api.getgems.io/graphql")

# Сколько сигналов максимум отправлять одному юзеру за один тик
MAX_DEALS_PER_USER = int(os.getenv("SCANNER_MAX_DEALS_PER_USER", "3"))


# ============ Утилиты нормализации и фильтров ============
def _safe_user_id(item) -> Optional[int]:
    if isinstance(item, int):
        return item
    if isinstance(item, dict):
        return item.get("user_id") or item.get("id")
    return None

def _hash_deal(deal: Dict[str, Any]) -> str:
    raw = (
        str(deal.get("id"))
        + "|"
        + str(deal.get("nft_address"))
        + "|"
        + str(deal.get("price_ton"))
        + "|"
        + str(deal.get("market"))
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _calc_discount_pct(deal: Dict[str, Any]) -> float:
    """Если есть fair/floor — считаем скидку, иначе используем уже проставленную."""
    fair = deal.get("fair_price_ton") or deal.get("floor_price_ton")
    try:
        fair = float(fair) if fair is not None else None
    except Exception:
        fair = None
    try:
        price = float(deal.get("price_ton") or 0.0)
    except Exception:
        price = 0.0
    if fair and fair > 0:
        return max(0.0, (1.0 - price / fair) * 100.0)
    return float(deal.get("discount_pct") or 0.0)

def _passes_filters_with_reason(deal: Dict[str, Any], st: Dict[str, Any]) -> Tuple[bool, str]:
    """Возвращаем True/False и причину отсева (для лога)."""
    # Порог скидки
    min_disc = float(st.get("min_discount_pct") or st.get("min_discount") or 0)
    disc = _calc_discount_pct(deal)
    if disc + 1e-9 < min_disc:
        return False, f"discount {disc:.1f}% < {min_disc:.0f}%"

    # Цена
    try:
        p = float(deal.get("price_ton") or 0.0)
    except Exception:
        p = 0.0
    min_price = st.get("min_price_ton")
    if min_price not in (None, ""):
        try:
            if p + 1e-9 < float(min_price):
                return False, f"price {p:.3f} < min {float(min_price):.3f}"
        except Exception:
            pass
    max_price = st.get("max_price_ton")
    if max_price not in (None, ""):
        try:
            if p - 1e-9 > float(max_price):
                return False, f"price {p:.3f} > max {float(max_price):.3f}"
        except Exception:
            pass

    # Коллекции
    cols = st.get("collections") or []
    if cols:
        col = str(deal.get("collection") or deal.get("collection_address") or "").lower()
        if col and col not in {c.lower() for c in cols}:
            return False, f"collection {col} not in allowlist"
    return True, "ok"

def _format_deal_msg(deal: Dict[str, Any]) -> str:
    name = deal.get("name") or deal.get("nft_name") or "NFT"
    market = deal.get("market") or "market"
    coll = deal.get("collection") or deal.get("collection_address") or "—"
    try:
        price = float(deal.get("price_ton") or 0.0)
    except Exception:
        price = 0.0
    disc = _calc_discount_pct(deal)
    url = deal.get("url") or deal.get("link") or ""

    lines = [
        f"🧩 <b>{name}</b>",
        f"🏷 Рынок: {market}",
        f"📦 Коллекция: <code>{coll}</code>",
        f"💰 Цена: <b>{price:.3f} TON</b>",
    ]
    if disc > 0:
        lines.append(f"📉 Скидка: <b>{disc:.0f}%</b>")
    if url:
        lines.append(f"\n<a href=\"{url}\">Открыть лот</a>")
    return "\n".join(lines)


# ============ Источники ============

async def _fetch_from_tonapi() -> List[Dict[str, Any]]:
    """
    TonAPI: пытаемся получить список активных ордеров.
    Если токен не задан — возвращаем [].
    """
    if not TONAPI_TOKEN:
        logger.debug("TONAPI_TOKEN не задан — TonAPI пропускаем.")
        return []

    url_candidates = [
        f"{TONAPI_BASE}/v2/marketplace/orders?limit=100",
        f"{TONAPI_BASE}/v2/market/active-orders?limit=100",
    ]
    headers = {"Authorization": f"Bearer {TONAPI_TOKEN}"}

    async with httpx.AsyncClient(timeout=10) as client:
        for url in url_candidates:
            try:
                r = await client.get(url, headers=headers)
                if r.status_code != 200:
                    logger.debug(f"TonAPI {url} -> {r.status_code} {r.text[:200]}")
                    continue
                data = r.json()
                items_raw = (
                    data.get("orders")
                    or data.get("items")
                    or data.get("nft_items")
                    or []
                )
                items: List[Dict[str, Any]] = []
                for it in items_raw:
                    price_ton = (
                        it.get("price_ton")
                        or (it.get("price", {}).get("value") if isinstance(it.get("price"), dict) else None)
                        or it.get("price")
                    )
                    try:
                        price_ton = float(price_ton) if price_ton is not None else None
                    except Exception:
                        price_ton = None

                    items.append({
                        "id": it.get("id") or it.get("order_id") or it.get("nft_item_id") or it.get("address"),
                        "nft_address": it.get("nft_address") or it.get("address"),
                        "name": it.get("name") or it.get("nft_name") or "",
                        "collection": (
                            (it.get("collection") or {}).get("address")
                            if isinstance(it.get("collection"), dict)
                            else it.get("collection")
                        ),
                        "market": it.get("market") or it.get("source") or "TonAPI",
                        "price_ton": price_ton,
                        "fair_price_ton": it.get("fair_price_ton") or it.get("floor_price_ton"),
                        "discount_pct": it.get("discount_pct"),
                        "url": it.get("url") or it.get("link"),
                    })
                if items:
                    logger.info(f"TonAPI: получено {len(items)} ордеров")
                    return items
            except Exception as e:
                logger.debug(f"TonAPI error {url}: {e}")
    return []


async def _fetch_from_getgems() -> List[Dict[str, Any]]:
    """
    Getgems GraphQL: пробуем получить активные ордера.
    Схема у Getgems меняется, поэтому парсим осторожно и логируем ответ.
    """
    if not GETGEMS_ENABLED:
        return []

    query = """
    query ListActiveOrders($limit:Int!) {
      orders: marketplaceOrders(limit: $limit, offset: 0, sort: {createdAt: DESC}, filter: {status: ACTIVE}) {
        id
        price
        nftItem {
          address
          name
          collection { address }
        }
        url
      }
    }
    """
    variables = {"limit": 100}

    async with httpx.AsyncClient(timeout=12) as client:
        try:
            r = await client.post(GETGEMS_GRAPHQL, json={"query": query, "variables": variables})
            if r.status_code != 200:
                logger.debug(f"Getgems GQL -> {r.status_code} {r.text[:200]}")
                return []
            data = r.json()
            nodes = (
                data.get("data", {}).get("orders")
                or data.get("data", {}).get("marketplaceOrders")
                or []
            )
            items: List[Dict[str, Any]] = []
            for it in nodes:
                nft = it.get("nftItem") or {}
                coll = (nft.get("collection") or {}).get("address")
                price = it.get("price")
                try:
                    price = float(price) if price is not None else None
                except Exception:
                    price = None
                items.append({
                    "id": it.get("id"),
                    "nft_address": nft.get("address"),
                    "name": nft.get("name") or "",
                    "collection": coll,
                    "market": "Getgems",
                    "price_ton": price,
                    "fair_price_ton": None,
                    "discount_pct": None,
                    "url": it.get("url"),
                })
            if items:
                logger.info(f"Getgems: получено {len(items)} ордеров")
            return items
        except Exception as e:
            logger.debug(f"Getgems error: {e}")
            return []


async def _fetch_all_sources() -> List[Dict[str, Any]]:
    """Грузим с нескольких источников и мержим (по (market,id,nft_address,price))."""
    sources: List[List[Dict[str, Any]]] = []

    tonapi_items = await _fetch_from_tonapi()
    if tonapi_items:
        sources.append(tonapi_items)

    getgems_items = await _fetch_from_getgems()
    if getgems_items:
        sources.append(getgems_items)

    # Мёрджим
    seen = set()
    out: List[Dict[str, Any]] = []
    for arr in sources:
        for d in arr:
            key = (d.get("market"), d.get("id"), d.get("nft_address"), d.get("price_ton"))
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
    logger.info(f"Суммарно получено {sum(len(a) for a in sources)} / уникальных {len(out)} ордеров")
    return out


# ============ Нотификация и цикл ============
async def _notify_user(bot: Bot, user_id: int, deals: List[Dict[str, Any]]):
    sent = 0
    for d in deals:
        if sent >= MAX_DEALS_PER_USER:
            break

        deal_hash = _hash_deal(d)
        try:
            if await was_deal_seen(deal_hash):
                continue
        except Exception:
            # Если БД недоступна — всё равно пробуем слать, но без дедупа
            pass

        msg = _format_deal_msg(d)
        try:
            await bot.send_message(user_id, msg, disable_web_page_preview=False)
            sent += 1
        except Exception as e:
            logger.warning(f"Не удалось отправить сообщение {user_id}: {e}")

        try:
            await mark_deal_seen({
                "deal_id": deal_hash,
                "url": d.get("url"),
                "collection": d.get("collection"),
                "name": d.get("name"),
                "price_ton": d.get("price_ton"),
                "floor_ton": d.get("fair_price_ton") or d.get("floor_price_ton"),
                "discount": _calc_discount_pct(d),
            })
        except Exception:
            pass


async def scanner_tick(bot: Bot):
    # 1) пользователи
    try:
        users = await get_scanner_users()
    except Exception as e:
        logger.warning(f"get_scanner_users() failed: {e}")
        users = []

    if not users:
        logger.debug("Нет включённых пользователей — тик пропущен.")
        return

    # 2) загрузка ордеров
    all_deals = await _fetch_all_sources()
    if not all_deals:
        logger.info("Источники вернули пусто — сигналов нет на этом тике.")
        return

    # 3) по пользователям — фильтр + лог причин отсева
    for u in users:
        user_id = _safe_user_id(u)
        if not user_id:
            continue

        try:
            st = await get_or_create_scanner_settings(user_id)
        except Exception as e:
            logger.warning(f"get_or_create_scanner_settings({user_id}) failed: {e}")
            continue

        filtered: List[Dict[str, Any]] = []
        rejected_reasons: Dict[str, int] = {}

        for d in all_deals:
            ok, reason = _passes_filters_with_reason(d, st)
            if ok:
                filtered.append(d)
            else:
                rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1

        logger.info(
            f"user {user_id}: прошло {len(filtered)}, "
            f"отсечено {sum(rejected_reasons.values())} ({', '.join(f'{k}:{v}' for k,v in list(rejected_reasons.items())[:5])})"
        )

        if not filtered:
            continue

        await _notify_user(bot, user_id, filtered)


async def scanner_loop():
    bot = Bot(token=settings.BOT_TOKEN, parse_mode="HTML")
    logger.info("Scanner loop started")

    async def _calc_sleep_default() -> int:
        try:
            users = await get_scanner_users()
            mins = []
            for u in users or []:
                uid = _safe_user_id(u)
                if not uid:
                    continue
                st = await get_or_create_scanner_settings(uid)
                mins.append(int(st.get("poll_seconds") or 60))
            if mins:
                return max(10, min(mins))
        except Exception:
            pass
        return DEFAULT_TICK_SECONDS

    sleep_seconds = await _calc_sleep_default()

    while True:
        try:
            await scanner_tick(bot)
        except Exception as e:
            logger.exception(f"scanner_tick crashed: {e}")

        try:
            sleep_seconds = await _calc_sleep_default()
        except Exception:
            pass

        await asyncio.sleep(sleep_seconds)
