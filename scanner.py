import os
import httpx
import logging

logger = logging.getLogger("nftbot")

TONAPI_KEY = os.getenv("TONAPI_KEY")
HEADERS = {"Authorization": f"Bearer {TONAPI_KEY}"} if TONAPI_KEY else {}

BASE_URL = "https://tonapi.io/v2"


async def get_collection_floor(collection_address: str) -> float:
    """Получить floor price коллекции (в TON)."""
    url = f"{BASE_URL}/nft/collections/{collection_address}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=HEADERS)
            if resp.status_code == 200:
                data = resp.json()
                return float(data.get("floor_price", 0)) / 1e9  # nanoton → TON
            else:
                logger.warning(f"Не удалось получить floor {collection_address}: {resp.text}")
    except Exception as e:
        logger.error(f"Ошибка получения floor: {e}")
    return 0.0


async def get_discounted_lots(collection_address: str, min_discount: float = 20.0, max_price: float = None):
    """
    Получить лоты коллекции, которые продаются со скидкой относительно floor.
    min_discount — минимальная скидка (в %).
    max_price — максимальная цена (в TON).
    """
    url = f"{BASE_URL}/nft/collections/{collection_address}/items?limit=50&sale_status=listed"

    results = []
    try:
        floor = await get_collection_floor(collection_address)
        if floor == 0:
            logger.warning(f"У коллекции {collection_address} нет floor, пропускаем")
            return []

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=HEADERS)
            if resp.status_code != 200:
                logger.warning(f"Ошибка при получении лотов {collection_address}: {resp.text}")
                return []

            items = resp.json().get("nft_items", [])
            for item in items:
                sale = item.get("sale")
                if not sale:
                    continue

                price = float(sale.get("price", 0)) / 1e9  # в TON
                if price <= 0:
                    continue

                discount = 100 * (1 - (price / floor)) if floor > 0 else 0

                if discount >= min_discount and (max_price is None or price <= max_price):
                    results.append({
                        "name": item.get("metadata", {}).get("name", "No name"),
                        "price": price,
                        "floor": floor,
                        "discount": discount,
                        "image": item.get("previews", [{}])[0].get("url", ""),
                        "url": f"https://getgems.io/asset/{item.get('address')}"
                    })

        results.sort(key=lambda x: x["discount"], reverse=True)
        return results

    except Exception as e:
        logger.error(f"Ошибка в get_discounted_lots: {e}")
        return []
