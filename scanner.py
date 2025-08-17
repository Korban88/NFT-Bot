# scanner.py
import asyncio
import logging
import os
import hashlib
from typing import List, Dict, Any, Optional

import httpx
from aiogram import Bot

from config import settings
from db import (
    get_scanner_users,
    get_or_create_scanner_settings,
    was_deal_seen,
    mark_deal_seen,
)

logger = logging.getLogger("nftbot.scanner")

# Интервал по умолчанию между тиками цикла, если у пользователя не задан poll_seconds
DEFAULT_TICK_SECONDS = int(os.getenv("SCANNER_TICK_SECONDS", "30"))


def _safe_user_id(item) -> Optional[int]:
    """get_scanner_users может вернуть список int или список dict'ов."""
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
    """Если API вернёт fair_price или floor_price — посчитаем. Иначе 0."""
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
        disc = max(0.0, (1.0 - price / fair) * 100.0)
        return disc
    return float(deal.get("discount_pct") or 0.0)


def _passes_filters(deal: Dict[str, Any], st: Dict[str, Any]) -> bool:
    # Скидка
    min_disc = float(st.get("min_discount_pct") or 0)
    if _calc_discount_pct(deal) + 1e-9 < min_disc:
        return False

    # Цена
    try:
        p = float(deal.get("price_ton") or 0.0)
    except Exception:
        p = 0.0

    min_price = st.get("min_price_ton")
    if min_price not in (None, ""):
        try:
            if p + 1e-9 < float(min_price):
                return False
        except Exception:
            pass

    max_price = st.get("max_price_ton")
    if max_price not in (None, ""):
        try:
            if p - 1e-9 > float(max_price):
                return False
        except Exception:
            pass

    # Коллекции
    cols = st.get("collections") or []
    if cols:
        col = str(deal.get("collection") or deal.get("collection_address") or "").lower()
        if col and col not in {c.lower() for c in cols}:
            return False

    return True


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


async def _fetch_from_tonapi() -> List[Dict[str, Any]]:
    """
    Заглушка-реализация запроса к TonAPI.
    Пытаемся обратиться к одному из известных маршрутов.
    Если ничего не доступно/неизвестно — возвращаем [].
    """
    token = getattr(settings, "TONAPI_TOKEN", None) or os.getenv("TONAPI_TOKEN")
    if not token:
        logger.debug("TONAPI_TOKEN не задан — пропускаю тик.")
        return []

    headers = {"Authorization": f"Bearer {token}"}

    # Кандидаты эндпоинтов (будем пробовать по очереди; формат ответа разный — приводим к общему).
    endpoints = [
        # гипотетический список маркет-ордеров:
        "https://tonapi.io/v2/marketplace/orders?limit=50",
        # запасной вариант (если другой маршрут):
        "https://tonapi.io/v2/market/active-orders?limit=50",
    ]

    async with httpx.AsyncClient(timeout=10) as client:
        for url in endpoints:
            try:
                r = await client.get(url, headers=headers)
                if r.status_code != 200:
                    continue
                data = r.json()
                items = []

                # Нормализация под наши поля
                # Популярные варианты ключей в ответах — подстрахуемся:
                candidates = (
                    data.get("orders") or
                    data.get("items") or
                    data.get("nft_items") or
                    []
                )

                for it in candidates:
                    price_ton = (
                        it.get("price_ton")
                        or (it.get("price", {}).get("value") if isinstance(it.get("price"), dict) else None)
                        or it.get("price")
                    )
                    try:
                        price_ton = float(price_ton) if price_ton is not None else None
                    except Exception:
                        price_ton = None

                    deal = {
                        "id": it.get("id") or it.get("order_id") or it.get("nft_item_id") or it.get("address"),
                        "nft_address": it.get("nft_address") or it.get("address"),
                        "name": it.get("name") or it.get("nft_name") or "",
                        "collection": (
                            (it.get("collection") or {}).get("address")
                            if isinstance(it.get("collection"), dict)
                            else it.get("collection")
                        ),
                        "market": it.get("market") or it.get("source") or "ton",
                        "price_ton": price_ton,
                        "fair_price_ton": it.get("fair_price_ton") or it.get("floor_price_ton"),
                        "discount_pct": it.get("discount_pct"),
                        "url": it.get("url") or it.get("link"),
                    }
                    items.append(deal)

                if items:
                    return items
            except Exception:
                # не шумим — просто пробуем следующий
                continue

    return []


async def _notify_user(bot: Bot, user_id: int, deals: List[Dict[str, Any]]):
    # Отошлём до 3 свежих подходящих лотов за тик
    for d in deals[:3]:
        deal_hash = _hash_deal(d)
        try:
            if await was_deal_seen(user_id, deal_hash):
                continue
        except Exception:
            # Если БД недоступна — всё равно пробуем слать, но без дедупа
            pass

        msg = _format_deal_msg(d)
        try:
            await bot.send_message(user_id, msg, disable_web_page_preview=False)
        except Exception as e:
            logger.warning(f"Не удалось отправить сообщение {user_id}: {e}")

        try:
            await mark_deal_seen(user_id, deal_hash)
        except Exception:
            pass


async def scanner_tick(bot: Bot):
    """Один проход сканирования для всех включённых пользователей."""
    try:
        users = await get_scanner_users()
    except Exception as e:
        logger.warning(f"get_scanner_users() failed: {e}")
        users = []

    if not users:
        return

    # Грузим общий пул лотов один раз
    try:
        all_deals = await _fetch_from_tonapi()
    except Exception as e:
        logger.warning(f"TonAPI fetch failed: {e}")
        all_deals = []

    # По пользователям — фильтруем и шлём
    for u in users:
        user_id = _safe_user_id(u)
        if not user_id:
            continue

        try:
            st = await get_or_create_scanner_settings(user_id)
        except Exception as e:
            logger.warning(f"get_or_create_scanner_settings({user_id}) failed: {e}")
            continue

        # Только включённый сканер
        if not st.get("enabled"):
            continue

        filtered = [d for d in all_deals if _passes_filters(d, st)]
        if not filtered:
            continue

        await _notify_user(bot, user_id, filtered)


async def scanner_loop():
    """
    Главный фоновый цикл.
    Создаёт собственного бота (без конфликтов с основным), тикает с интервалом.
    """
    bot = Bot(token=settings.BOT_TOKEN, parse_mode="HTML")
    logger.info("Scanner loop started")

    # Базовый тик
    sleep_seconds = DEFAULT_TICK_SECONDS
    # Подстраиваемся под минимальный poll_seconds из включённых пользователей
    # (если не получится — останется DEFAULT_TICK_SECONDS)
    async def _calc_sleep_default() -> int:
        try:
            users = await get_scanner_users()
            mins = []
            for u in users or []:
                uid = _safe_user_id(u)
                if not uid:
                    continue
                st = await get_or_create_scanner_settings(uid)
                if st.get("enabled"):
                    mins.append(int(st.get("poll_seconds") or 60))
            if mins:
                return max(10, min(mins))
        except Exception:
            pass
        return DEFAULT_TICK_SECONDS

    # Первый расчёт
    sleep_seconds = await _calc_sleep_default()

    while True:
        try:
            await scanner_tick(bot)
        except Exception as e:
            logger.exception(f"scanner_tick crashed: {e}")

        # Периодически пересчитаем интервал — вдруг пользователь менял настройки
        try:
            sleep_seconds = await _calc_sleep_default()
        except Exception:
            pass

        await asyncio.sleep(sleep_seconds)
