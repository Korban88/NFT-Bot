# scanner.py
import os
import asyncio
import hashlib
import logging
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

import httpx

from db import (
    get_pool,
    get_scanner_users,
    get_or_create_scanner_settings,
    was_deal_seen,
    mark_deal_seen,
)

log = logging.getLogger("nftbot.scanner")

# ===== ENV =====
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))

# источники
GETGEMS_ENABLED = os.getenv("GETGEMS_ENABLED", "0") == "1"
TONAPI_REST_ENABLED = os.getenv("TONAPI_REST_ENABLED", "0") == "1"

# dTON
DTON_ENABLED = os.getenv("DTON_ENABLED", "0") == "1"
DTON_API_KEY = os.getenv("DTON_API_KEY", "")
DTON_BASE = os.getenv("DTON_BASE", "https://dton.io").rstrip("/")
DTON_PAGE_SIZE = int(os.getenv("DTON_PAGE_SIZE", "50"))
# сколько минут назад смотреть завершённые сделки (dTON raw_transactions)
DTON_LOOKBACK_MIN = int(os.getenv("DTON_LOOKBACK_MIN", "60"))

TONAPI_KEY = os.getenv("TONAPI_KEY") or os.getenv("TONAPI_TOKEN") or ""
HEADERS_TONAPI = {"Authorization": f"Bearer {TONAPI_KEY}"} if TONAPI_KEY else {}

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

# ===== dTON =====
# Используем dTON GraphQL:
# - таблица raw_transactions — завершённые продажи NFT с распарсенными полями цены и коллекции
# - фильтры: parsed_seller_is_closed=1, account_state_state_init_code_has_get_nft_data=1, gen_utime__gt
# Источники: docs/статьи dTON с примерами полей parsed_* и фильтров.

async def _fetch_from_dton() -> List[Dict[str, Any]]:
    if not DTON_ENABLED:
        return []

    if not DTON_API_KEY:
        log.warning("dTON включен, но DTON_API_KEY пуст — пропускаем источник.")
        return []

    url = f"{DTON_BASE}/{DTON_API_KEY}/graphql"

    # берём сделки за последний интервал
    since = datetime.now(timezone.utc) - timedelta(minutes=DTON_LOOKBACK_MIN)
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")

    # Минимальный безопасный запрос по примерам dTON:
    # - parsed_seller_is_closed: 1  — завершённые продажи
    # - account_state_state_init_code_has_get_nft_data: 1 — это точно NFT
    # - gen_utime__gt — ограничение по времени
    # Вытаскиваем адрес NFT и коллекции в friendly-виде + цену (в nanoTON).
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

    variables = {
        "gt": since_str,
        "pageSize": DTON_PAGE_SIZE,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json={"query": gql, "variables": variables})
            log.info("HTTP dTON POST %s -> %s", url, r.status_code)
            data = r.json()
    except Exception as e:
        log.warning("dTON: запрос упал: %s", e)
        return []

    errors = (data or {}).get("errors")
    if errors:
        log.info("dTON -> ошибки GraphQL: %s", errors)
        return []

    rows = (data or {}).get("data", {}).get("raw_transactions", []) or []
    deals: List[Dict[str, Any]] = []

    for it in rows:
        nft_addr = it.get("nft_address")  # friendly адрес NFT
        col_addr = it.get("col_address")  # friendly адрес коллекции
        price_nano = it.get("price")

        price_ton = _as_ton(price_nano)
        deal_url = f"https://tonviewer.com/{nft_addr}" if nft_addr else None

        deal = {
            "deal_id": _deal_id("dton", nft_addr or "", str(price_nano or "")),
            "url": deal_url,
            "collection": col_addr or "",
            "name": nft_addr or "NFT",
            "price_ton": price_ton,
            "floor_ton": None,      # dTON здесь не даёт floor — фильтруем по цене/коллекциям
            "discount": None,       # скидку не считаем на этом источнике
            "source": "dton",
        }
        deals.append(deal)

    return deals

# ===== (заглушки для прочих источников — оставляем как были/минимальные) =====

async def _fetch_from_tonapi_rest() -> List[Dict[str, Any]]:
    if not TONAPI_REST_ENABLED:
        return []
    # На текущем этапе TonAPI REST отключён (старые эндпоинты 404).
    return []

async def _fetch_from_getgems() -> List[Dict[str, Any]]:
    if not GETGEMS_ENABLED:
        return []
    # Мы отключили Getgems GraphQL — схема изменилась и старые поля недоступны.
    return []

# ===== ОБЩИЙ СКАН =====

def _passes_user_filters(deal: Dict[str, Any], st: Dict[str, Any]) -> bool:
    """Сопоставление сделки с настройками пользователя."""
    min_discount: Optional[float] = st.get("min_discount")
    max_price_ton = st.get("max_price_ton")
    collections = st.get("collections")  # может быть None или list[str]

    # Фильтр по коллекциям
    if collections:
        # сравниваем friendly-адреса (в БД мы храним как есть)
        if not any(
            (deal.get("collection") or "").lower() == (c or "").lower()
            for c in collections
        ):
            return False

    # Фильтр по цене (если цена известна)
    price = deal.get("price_ton")
    if max_price_ton is not None and price is not None:
        try:
            if Decimal(str(price)) > Decimal(str(max_price_ton)):
                return False
        except Exception:
            pass

    # Фильтр по скидке (если её нет — пропускаем только если min_discount <= 0)
    disc = deal.get("discount")
    if disc is None:
        return (min_discount is None) or (float(min_discount) <= 0.0)
    else:
        try:
            return float(disc) >= float(min_discount or 0.0)
        except Exception:
            return True

async def _notify_user(user_id: int, deal: Dict[str, Any]):
    from aiogram import Bot
    TOKEN = os.getenv("BOT_TOKEN", "")
    if not TOKEN:
        return

    bot = Bot(TOKEN, parse_mode="HTML")
    try:
        parts = []
        parts.append(f"🧩 <b>Сигнал (dTON)</b>")
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
    finally:
        await bot.session.close()

async def _scan_once() -> None:
    pool = await get_pool()

    # собираем ордера со всех активных источников
    batches: List[List[Dict[str, Any]]] = []

    # dTON (активен)
    try:
        dton = await _fetch_from_dton()
        batches.append(dton)
    except Exception as e:
        log.warning("dTON fetch failed: %s", e)

    # другие источники (если включены)
    try:
        tonapi = await _fetch_from_tonapi_rest()
        batches.append(tonapi)
    except Exception as e:
        log.warning("TonAPI REST fetch failed: %s", e)

    try:
        gg = await _fetch_from_getgems()
        batches.append(gg)
    except Exception as e:
        log.warning("Getgems fetch failed: %s", e)

    # плоский список и дедупликация по deal_id
    all_items = [it for batch in batches for it in (batch or [])]
    uniq: Dict[str, Dict[str, Any]] = {}
    for it in all_items:
        did = it.get("deal_id")
        if not did:
            continue
        uniq[did] = it

    log.info("Суммарно получено %d / уникальных %d ордеров", len(all_items), len(uniq))

    if not uniq:
        log.info("Нет активных источников или источники вернули пусто.")
        return

    users = await get_scanner_users(pool)
    if not users:
        return

    for deal in uniq.values():
        # антидубликаты на уровне БД
        if await was_deal_seen(pool, deal["deal_id"]):
            continue

        # для каждого пользователя — проверка его фильтров и отправка
        for uid in users:
            st = await get_or_create_scanner_settings(pool, uid)
            if _passes_user_filters(deal, st):
                await _notify_user(uid, deal)

        # помечаем как отправленное
        await mark_deal_seen(pool, deal)

async def scanner_loop():
    log.info("Scanner loop started")
    while True:
        try:
            await _scan_once()
        except Exception as e:
            log.exception("scanner tick failed: %s", e)
        await asyncio.sleep(POLL_SECONDS)
