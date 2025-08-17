# scanner.py
import os
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from db import (
    get_pool,
    get_scanner_users,
    get_or_create_scanner_settings,
    was_deal_seen,
    mark_deal_seen,
)

log = logging.getLogger("nftbot.scanner")

# -------- Параметры источников --------
GETGEMS_GRAPHQL_URL = "https://api.getgems.io/graphql"
GETGEMS_ENABLED = True  # единственный активный источник на сейчас

# В будущем можно снова включить TonAPI, но их старые REST marketplace-эндпоинты 404
TONAPI_MARKET_ENABLED = False  # не используем
# Если понадобятся запросы, ключ берём из переменной окружения
TONAPI_KEY = os.getenv("TONAPI_KEY") or os.getenv("TONAPI_TOKEN") or ""

# --------- Вспомогательные утилиты ----------

def _norm_ton(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None

def _deal_id(source: str, raw: Dict[str, Any]) -> str:
    """
    Делаем детерминированный идентификатор сделки на базе источника и id/адреса.
    """
    base = raw.get("id") or raw.get("orderId") or raw.get("nftAddress") or raw.get("address") or ""
    return f"{source}:{base}"

# --------- Getgems (GraphQL) ----------

# Несколько вариантов «плоских» запросов — в некоторых ревизиях схемы поля назывались по-разному.
# Мы пробуем по очереди, пока один не вернётся без errors.
_GG_QUERIES: List[Tuple[str, str]] = [
    (
        "marketplaceOrders",
        """
        query GetOrders($limit:Int!, $offset:Int!) {
          marketplaceOrders(limit:$limit, offset:$offset, filter:{status:ACTIVE}, sort:{createdAt:DESC}) {
            total
            items {
              id
              url
              price          # TON price (numeric)
              nftItem {
                address
                name
                collection { address name }
              }
              createdAt
            }
          }
        }
        """,
    ),
    (
        "activeOrders",
        """
        query GetOrders($limit:Int!, $offset:Int!) {
          activeOrders(limit:$limit, offset:$offset) {
            total
            items {
              id
              url
              price
              nftItem {
                address
                name
                collection { address name }
              }
              createdAt
            }
          }
        }
        """,
    ),
    (
        "orders",
        """
        query GetOrders($limit:Int!, $offset:Int!) {
          orders(limit:$limit, offset:$offset, filter:{status:ACTIVE}) {
            total
            items {
              id
              url
              price
              nft {          # иногда поле называется nft
                address
                name
                collection { address name }
              }
              createdAt
            }
          }
        }
        """,
    ),
]

async def _getgems_fetch_orders(client: httpx.AsyncClient, limit: int = 100) -> List[Dict[str, Any]]:
    if not GETGEMS_ENABLED:
        return []

    variables = {"limit": limit, "offset": 0}
    headers = {"Content-Type": "application/json"}
    last_error_text = None

    for root_field, query in _GG_QUERIES:
        try:
            resp = await client.post(GETGEMS_GRAPHQL_URL, json={"query": query, "variables": variables}, headers=headers, timeout=15)
            if resp.status_code != 200:
                last_error_text = f"HTTP {resp.status_code} {resp.text[:400]}"
                log.info("Getgems GraphQL try %s -> %s", root_field, last_error_text)
                continue

            data = resp.json()
            if "errors" in data:
                last_error_text = f"errors: {data['errors']}"
                log.info("Getgems GraphQL try %s -> %s", root_field, last_error_text)
                continue

            payload = data.get("data", {})
            block = payload.get(root_field)
            if not block or not isinstance(block, dict):
                # может быть вложенность типа marketplace { activeOrders {...} }
                marketplace = payload.get("marketplace")
                if isinstance(marketplace, dict):
                    block = marketplace.get(root_field)

            if not block or "items" not in block:
                last_error_text = f"no items at field '{root_field}'"
                log.info("Getgems GraphQL try %s -> %s", root_field, last_error_text)
                continue

            items = block.get("items") or []
            normed: List[Dict[str, Any]] = []
            for it in items:
                nft = it.get("nftItem") or it.get("nft") or {}
                collection = (nft or {}).get("collection") or {}
                deal = {
                    "source": "getgems",
                    "deal_id": _deal_id("getgems", it),
                    "url": it.get("url"),
                    "collection": collection.get("name") or collection.get("address") or "",
                    "name": nft.get("name") or nft.get("address"),
                    "nft_address": nft.get("address"),
                    "price_ton": _norm_ton(it.get("price")),
                    # пол площадки мы не знаем из этого запроса надёжно — вычислим позже отдельно при необходимости
                    "floor_ton": None,
                    "discount": None,
                }
                normed.append(deal)
            return normed

        except Exception as e:
            last_error_text = f"exc: {e}"
            log.exception("Getgems GraphQL try %s failed", root_field)

    if last_error_text:
        log.warning("Getgems GraphQL: all variants failed, last: %s", last_error_text)
    return []

# --------- Публикация сигналов ---------

async def _emit_deals_for_user(user_id: int, deals: List[Dict[str, Any]], settings: Dict[str, Any]):
    """
    Фильтрация по настройкам пользователя и антидубликаты.
    Пока floor неизвестен, фильтруем только по max_price.
    """
    pool = await get_pool()
    max_price = settings.get("max_price_ton")  # Decimal -> приводить к float не обязательно
    min_discount = settings.get("min_discount")  # пока может быть None — скидку вычислим позже

    for d in deals:
        # фильтр по цене
        if max_price is not None and d.get("price_ton") is not None:
            try:
                if float(d["price_ton"]) > float(max_price):
                    continue
            except Exception:
                pass

        # TODO: добавить расчёт скидки при наличии floor
        d["discount"] = None

        # антидубликаты
        if await was_deal_seen(pool, d["deal_id"]):
            continue

        # Метка «смотрели»
        await mark_deal_seen(pool, d)

        # Здесь можно отправлять сообщение пользователю (через бота) — сейчас у нас сканер без прямой привязки к Bot API.
        log.info("Found deal for user %s: %s | %s TON | %s", user_id, d.get("name"), d.get("price_ton"), d.get("url"))

# --------- Основной цикл ---------

async def scanner_loop():
    log.info("Scanner loop started")

    # единый httpx-клиент на все запросы
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            t0 = time.monotonic()
            try:
                pool = await get_pool()
                users: List[int] = await get_scanner_users(pool)
            except Exception as e:
                log.warning("get_scanner_users() failed: %s", e)
                users = []

            # тянем с источников
            all_orders: List[Dict[str, Any]] = []

            # Только Getgems (TonAPI market временно выключен — старые пути 404)
            try:
                g = await _getgems_fetch_orders(client, limit=100)
                all_orders.extend(g)
            except Exception:
                log.exception("getgems fetch failed")

            # дедупликация по deal_id
            seen = set()
            unique_orders = []
            for o in all_orders:
                did = o.get("deal_id")
                if not did:
                    continue
                if did in seen:
                    continue
                seen.add(did)
                unique_orders.append(o)

            log.info("Суммарно получено %s / уникальных %s ордеров", len(all_orders), len(unique_orders))

            if not unique_orders:
                log.info("Источники вернули пусто — сигналов нет на этом тике.")

            # разослать по пользователям согласно их настройкам
            for uid in users:
                try:
                    st = await get_or_create_scanner_settings(pool, uid)
                    await _emit_deals_for_user(uid, unique_orders, st)
                except Exception:
                    log.exception("emit for user %s failed", uid)

            # интервал тика: берём минимальный из пользовательских, иначе 60с по умолчанию
            sleep_sec = 60
            if users:
                try:
                    # забираем минимальный poll_seconds среди пользователей (если поле есть в вашей схеме)
                    # иначе используем 60
                    mins: List[int] = []
                    for uid in users:
                        st = await get_or_create_scanner_settings(pool, uid)
                        ps = st.get("poll_seconds")
                        if isinstance(ps, int) and ps > 0:
                            mins.append(ps)
                    if mins:
                        sleep_sec = max(10, min(mins))
                except Exception:
                    pass

            dt = time.monotonic() - t0
            wait_left = max(5, sleep_sec - int(dt))
            await asyncio.sleep(wait_left)
