# scanner.py
import os
import asyncio
import hashlib
import logging
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

import httpx
from aiogram import Bot

from db import (
    get_scanner_users,               # () -> List[int]
    get_or_create_scanner_settings,  # (user_id) -> Dict
    was_deal_seen,                   # (deal_id, url=None) -> bool
    mark_deal_seen,                  # (deal_dict) -> None
)

log = logging.getLogger("nftbot.scanner")

# ===== ENV =====
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))

# источники (TonAPI REST / Getgems оставлены выключенными)
GETGEMS_ENABLED = os.getenv("GETGEMS_ENABLED", "0") == "1"
TONAPI_REST_ENABLED = os.getenv("TONAPI_REST_ENABLED", "0") == "1"

# dTON
DTON_ENABLED = os.getenv("DTON_ENABLED", "1") == "1"
DTON_API_KEY = os.getenv("DTON_API_KEY", "")
DTON_BASE = os.getenv("DTON_BASE", "https://dton.io").rstrip("/")
DTON_PAGE_SIZE = int(os.getenv("DTON_PAGE_SIZE", "50"))
DTON_LOOKBACK_MIN = int(os.getenv("DTON_LOOKBACK_MIN", "60"))

# ===== УТИЛИТЫ =====

def _deal_id(*parts: str) -> str:
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]
    return f"deal_{h}"

def _as_ton(nano: Optional[int]) -> Optional[float]:
    if nano is None:
        return None
    try:
        return float(Decimal(nano) / Decimal(1_000_000_000))
    except Exception:
        return None

# ===== dTON: закрытые продажи за последний интервал =====

async def _fetch_from_dton() -> List[Dict[str, Any]]:
    if not DTON_ENABLED or not DTON_API_KEY:
        return []

    url = f"{DTON_BASE}/{DTON_API_KEY}/graphql"
    since = datetime.now(timezone.utc) - timedelta(minutes=DTON_LOOKBACK_MIN)
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")

    gql = """
    query FetchClosedNftSales($gt: String!, $pageSize: Int!) {
      raw_transactions(
        parsed_seller_is_closed: 1
        account_state_state_init_code_has_get_nft_data: 1
        gen_utime__gt: $gt
        page: 0
        page_size: $pageSize
        order_by: "gen_utime"
        order_desc: true
      ) {
        nft_address: address__friendly
        col_address: parsed_nft_collection_address_address__friendly
        price: parsed_seller_nft_price
      }
    }
    """
    variables = {"gt": since_str, "pageSize": DTON_PAGE_SIZE}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json={"query": gql, "variables": variables})
            log.info("HTTP dTON POST %s -> %s", url, r.status_code)
            data = r.json()
    except Exception as e:
        log.warning("dTON: запрос упал: %s", e)
        return []

    if (data or {}).get("errors"):
        log.info("dTON -> ошибки GraphQL: %s", data["errors"])
        return []

    rows = (data or {}).get("data", {}).get("raw_transactions", []) or []
    deals: List[Dict[str, Any]] = []
    for it in rows:
        nft_addr = it.get("nft_address")
        col_addr = it.get("col_address")
        price_nano = it.get("price")
        price_ton = _as_ton(price_nano)
        deal_url = f"https://tonviewer.com/{nft_addr}" if nft_addr else None

        deals.append({
            "deal_id": _deal_id("dton", nft_addr or "", str(price_nano or "")),
            "url": deal_url,
            "collection": col_addr or "",
            "name": nft_addr or "NFT",
            "price_ton": price_ton,
            "floor_ton": None,     # floor пока не считаем
            "discount": None,      # скидки нет без floor
            "source": "dton",
        })
    return deals

# ===== заглушки других источников (выкл по умолчанию) =====

async def _fetch_from_tonapi_rest() -> List[Dict[str, Any]]:
    return [] if not TONAPI_REST_ENABLED else []

async def _fetch_from_getgems() -> List[Dict[str, Any]]:
    return [] if not GETGEMS_ENABLED else []

# ===== ФИЛЬТРЫ/НОТИФИКАЦИИ =====

def _passes_user_filters(deal: Dict[str, Any], st: Dict[str, Any]) -> bool:
    """
    Сейчас отправляем по коллекциям/цене.
    Для discount — если None, то пропускаем только когда min_discount <= 0.
    """
    min_discount: Optional[float] = st.get("min_discount")
    max_price_ton = st.get("max_price_ton")
    collections = st.get("collections")

    if collections:
        allow = {str(c or "").lower() for c in collections}
        if (deal.get("collection") or "").lower() not in allow:
            return False

    price = deal.get("price_ton")
    if max_price_ton is not None and price is not None:
        try:
            if Decimal(str(price)) > Decimal(str(max_price_ton)):
                return False
        except Exception:
            pass

    disc = deal.get("discount")
    if disc is None:
        return (min_discount is None) or (float(min_discount) <= 0.0)
    try:
        return float(disc) >= float(min_discount or 0.0)
    except Exception:
        return True

async def _notify_user(bot: Bot, user_id: int, deal: Dict[str, Any]):
    parts = ["🧩 <b>Сигнал (dTON)</b>"]
    if deal.get("name"):
        parts.append(f"• NFT: <code>{deal['name']}</code>")
    if deal.get("collection"):
        parts.append(f"• Коллекция: <code>{deal['collection']}</code>")
    if deal.get("price_ton") is not None:
        parts.append(f"• Цена: <b>{deal['price_ton']:.4f} TON</b>")
    if deal.get("discount") is not None:
        parts.append(f"• Скидка: <b>{deal['discount']:.1f}%</b>")
    if deal.get("url"):
        parts.append(f"\n➡️ <a href=\"{deal['url']}\">Открыть в Tonviewer</a>")
    text = "\n".join(parts)
    await bot.send_message(user_id, text, disable_web_page_preview=True)

# ===== ОДИН ТИК СКАНЕРА =====

async def _scan_once(bot: Optional[Bot]) -> None:
    # 1) собрать сделки с источников
    batches: List[List[Dict[str, Any]]] = []
    try:
        batches.append(await _fetch_from_dton())
    except Exception as e:
        log.warning("dTON fetch failed: %s", e)

    # (оставлено на будущее)
    try:
        batches.append(await _fetch_from_tonapi_rest())
    except Exception as e:
        log.warning("TonAPI REST fetch failed: %s", e)
    try:
        batches.append(await _fetch_from_getgems())
    except Exception as e:
        log.warning("Getgems fetch failed: %s", e)

    all_items = [it for batch in batches for it in (batch or [])]
    uniq: Dict[str, Dict[str, Any]] = {}
    for it in all_items:
        did = it.get("deal_id")
        if not did:
            continue
        uniq[did] = it

    log.info("Суммарно получено %d / уникальных %d ордеров", len(all_items), len(uniq))
    if not uniq:
        return

    # 2) список пользователей
    users = await get_scanner_users()
    if not users:
        log.info("Нет включённых пользователей — рассылка пропущена.")
        return
    log.info("Включённых пользователей: %d", len(users))

    # 3) фильтрация и отправка
    sent_total = 0
    for deal in uniq.values():
        # Если уже видели этот URL/лот — пропускаем
        if await was_deal_seen(deal["deal_id"], deal.get("url")):
            continue

        delivered_to_any = False
        for uid in users:
            st = await get_or_create_scanner_settings(uid)
            if _passes_user_filters(deal, st):
                if bot:
                    try:
                        await _notify_user(bot, uid, deal)
                        delivered_to_any = True
                        sent_total += 1
                    except Exception as e:
                        log.warning("Send to %s failed: %s", uid, e)

        # ВАЖНО: помечаем в БД только если кому-то отправили
        if delivered_to_any:
            await mark_deal_seen(deal)

    log.info("Разослано сообщений: %d", sent_total)

# ===== ЦИКЛ =====

async def scanner_loop():
    log.info("Scanner loop started")

    token = os.getenv("BOT_TOKEN", "")
    bot: Optional[Bot] = Bot(token, parse_mode="HTML") if token else None
    if not bot:
        log.warning("BOT_TOKEN не задан — уведомления отправляться не будут.")

    while True:
        try:
            await _scan_once(bot)
        except Exception as e:
            log.exception("scanner tick failed: %s", e)
        await asyncio.sleep(POLL_SECONDS)
