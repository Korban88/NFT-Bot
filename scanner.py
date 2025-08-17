import os
import httpx
import logging

logger = logging.getLogger("nftbot")

TONAPI_KEY = os.getenv("TONAPI_KEY")
HEADERS = {"Authorization": f"Bearer {TONAPI_KEY}"} if TONAPI_KEY else {}

MIN_DISCOUNT = float(os.getenv("MIN_DISCOUNT", 20.0))  # %
MAX_PRICE_TON = os.getenv("MAX_PRICE_TON")
MAX_PRICE_TON = float(MAX_PRICE_TON) if MAX_PRICE_TON not in [None, "", "None"] else None

# список коллекций из переменных окружения
COLLECTIONS = os.getenv("NFT_COLLECTIONS", "")
COLLECTIONS = [c.strip() for c in COLLECTIONS.split(",") if c.strip()]


async def fetch_lots(collection):
    """
    Получаем список лотов по коллекции через TonAPI
    """
    url = f"https://tonapi.io/v2/nfts/collections/{collection}/auctions"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=HEADERS)
            r.raise_for_status()
            return r.json().get("auctions", [])
    except Exception as e:
        logger.error(f"Ошибка запроса TonAPI для {collection}: {e}")
        return []


async def scan_collections():
    """
    Основная функция сканера:
    собираем лоты, считаем скидку и фильтруем
    """
    results = []

    for collection in COLLECTIONS:
        lots = await fetch_lots(collection)

        for lot in lots:
            try:
                price = float(lot.get("price", 0)) / 1e9  # в TON
                floor = float(lot.get("nft", {}).get("collection", {}).get("floor_price", 0)) / 1e9

                if not price or not floor:
                    continue

                discount = (1 - price / floor) * 100

                # фильтры
                if discount < MIN_DISCOUNT:
                    continue
                if MAX_PRICE_TON and price > MAX_PRICE_TON:
                    continue

                results.append({
                    "collection": lot["nft"]["collection"]["name"],
                    "name": lot["nft"]["metadata"].get("name", "Без имени"),
                    "price": price,
                    "floor": floor,
                    "discount": discount,
                    "image": lot["nft"]["metadata"].get("image"),
                    "url": f"https://getgems.io/nft/{lot['nft']['address']}"
                })
            except Exception as e:
                logger.warning(f"Ошибка обработки лота: {e}")

    return sorted(results, key=lambda x: x["discount"], reverse=True)
