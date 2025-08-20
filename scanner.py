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
    get_scanner_users,               # () -> List[int]
    get_or_create_scanner_settings,  # (user_id) -> Dict
    was_deal_seen,                   # (deal_id) -> bool
    mark_deal_seen,                  # (deal_dict) -> None
)

logger = logging.getLogger("nftbot.scanner")

# ---------- конфиги ----------
DEFAULT_TICK_SECONDS = int(os.getenv("SCANNER_TICK_SECONDS", "30"))
MAX_DEALS_PER_USER = int(os.getenv("SCANNER_MAX_DEALS_PER_USER", "3"))

# Источники: «устаревшие» выключаем.
TONAPI_REST_ENABLED = os.getenv("TONAPI_REST_ENABLED", "0") == "1"   # 404 — оставлено на случай, если вернут API
GETGEMS_ENABLED = os.getenv("GETGEMS_ENABLED", "0") == "1"           # публичный GraphQL не даёт нужные поля

# dTON — новый источник (включаем)
DTON_ENABLED = os.getenv("DTON_ENABLED", "1") == "1"
DTON_API_KEY = os.getenv("DTON_API_KEY") or ""
DTON_ENDPOINT = f"https://dton.io/{DTON_API_KEY}/graphql" if DTON_API_KEY else None

# Code-hash контрактов фикс-прайс Getgems (основные ревизии)
GETGEMS_FIX_V4R1 = "6B95A6418B9C9D2359045D1E7559B8D549AE0E506F24CAAB58FA30C8FB1FEB86"
GETGEMS_FIX_V3R3 = "24221FA571E542E055C77BEDFDBF527C7AF460CFDC7F344C450787B4CFA1EB4D"
SALE_CODE_HASHES = [GETGEMS_FIX_V4R1, GETGEMS_FIX_V3R3]

# ---------- утилиты ----------

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
    min_disc = float(st.get("min_discount_pct") or st.get("min_discount") or 0)
    disc = _calc_discount_pct(deal)
    if disc + 1e-9 < min_disc:
        return False, f"discount {disc:.1f}% < {min_disc:.0f}%"

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

# ---------- источники ----------

async def _fetch_from_dton() -> List[Dict[str, Any]]:
    """
    dTON GraphQL: ищем аккаунты с code_hash из SALE_CODE_HASHES (контракты Getgems Fix-Price).
    На этом шаге мы *только* собираем активные sale-аккаунты (адреса); цену/NFT достанем на следующем шаге.
    """
    if not (DTON_ENABLED and DTON_ENDPOINT and DTON_API_KEY):
        return []

    # dTON схемы могут отличаться по названиям полей; пробуем несколько безопасных запросов.
    queries: List[Tuple[str, Dict[str, Any]]] = [
        (
            # Вариант 1: accounts(...) { address, code_hash, last_paid }
            """
            query Sales($hashes:[String!]!, $limit:Int!) {
              accounts(filter:{ code_hash:{in:$hashes} }, limit:$limit, orderBy: LAST_PAID_DESC) {
                address
                code_hash
                last_paid
              }
            }
            """,
            {"hashes": SALE_CODE_HASHES, "limit": 200},
        ),
        (
            # Вариант 2: accounts(...) { id, address }
            """
            query Sales($hashes:[String!]!, $limit:Int!) {
              accounts(filter:{ code_hash:{in:$hashes} }, limit:$limit) {
                id
                address
              }
            }
            """,
            {"hashes": SALE_CODE_HASHES, "limit": 200},
        ),
    ]

    found_addresses: List[str] = []
    async with httpx.AsyncClient(timeout=15) as client:
        for q, variables in queries:
            try:
                r = await client.post(DTON_ENDPOINT, json={"query": q, "variables": variables})
                if r.status_code != 200:
                    logger.info("dTON -> %s %s", r.status_code, r.text[:300])
                    continue
                data = r.json()
                if "errors" in data:
                    logger.info("dTON errors: %s", data["errors"])
                    continue
                items = (data.get("data") or {}).get("accounts") or []
                addrs = [it.get("address") for it in items if it.get("address")]
                if addrs:
                    found_addresses = addrs
                    break
            except Exception as e:
                logger.info("dTON fetch error: %s", e)

    if not found_addresses:
        logger.info("dTON: по code_hash ничего не нашли (возможно, отличается схема).")
        return []

    logger.info("dTON: найдено адресов sale-контрактов: %d", len(found_addresses))

    # Пока не знаем цену/коллекцию: вернём «черновые» сделки с минимальным составом.
    # На следующем шаге подцепим run-get и обогатим данными.
    deals: List[Dict[str, Any]] = []
    for addr in found_addresses[:200]:
        deals.append({
            "id": addr,
            "nft_address": None,
            "name": "Getgems Sale",
            "collection": None,
            "market": "dTON/Getgems",
            "price_ton": None,            # будет заполнено после run-get
            "fair_price_ton": None,
            "discount_pct": None,
            "url": f"https://tonviewer.com/{addr}",
        })
    return deals

async def _fetch_all_sources() -> List[Dict[str, Any]]:
    sources: List[List[Dict[str, Any]]] = []

    # dTON — основной источник
    dton_items = await _fetch_from_dton()
    if dton_items:
        sources.append(dton_items)

    # (Оставлено для совместимости — по умолчанию выключены)
    # if TONAPI_REST_ENABLED: sources.append(await _fetch_from_tonapi_rest())
    # if GETGEMS_ENABLED:     sources.append(await _fetch_from_getgems())

    if not sources:
        logger.info("Нет активных источников или источники вернули пусто.")
        return []

    # дедупликация
    seen = set()
    out: List[Dict[str, Any]] = []
    for arr in sources:
        for d in arr:
            key = (d.get("market"), d.get("id"))
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
    logger.info("Суммарно получено %s / уникальных %s ордеров", sum(len(a) for a in sources), len(out))
    return out

# ---------- отправка пользователю ----------

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
            pass

        msg = _format_deal_msg(d)
        try:
            await bot.send_message(user_id, msg, disable_web_page_preview=False)
            sent += 1
        except Exception as e:
            logger.warning("Send to %s failed: %s", user_id, e)

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

# ---------- основной цикл ----------

async def scanner_tick(bot: Bot):
    try:
        users = await get_scanner_users()
    except Exception as e:
        logger.warning("get_scanner_users() failed: %s", e)
        users = []

    if not users:
        logger.debug("Нет включённых пользователей — тик пропущен.")
        return

    all_deals = await _fetch_all_sources()
    if not all_deals:
        logger.info("Источники вернули пусто — сигналов нет на этом тике.")
        return

    for u in users:
        user_id = _safe_user_id(u)
        if not user_id:
            continue

        try:
            st = await get_or_create_scanner_settings(user_id)
        except Exception as e:
            logger.warning("get_or_create_scanner_settings(%s) failed: %s", user_id, e)
            continue

        filtered: List[Dict[str, Any]] = []
        reject: Dict[str, int] = {}

        for d in all_deals:
            ok, reason = _passes_filters_with_reason(d, st)
            if ok:
                filtered.append(d)
            else:
                reject[reason] = reject.get(reason, 0) + 1

        logger.info("user %s: прошло %s, отсечено %s (%s)",
                    user_id, len(filtered), sum(reject.values()),
                    ", ".join(f"{k}:{v}" for k, v in list(reject.items())[:5]))

        if filtered:
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
            logger.exception("scanner_tick crashed: %s", e)
        try:
            sleep_seconds = await _calc_sleep_default()
        except Exception:
            pass
        await asyncio.sleep(sleep_seconds)
