import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN_NFTBOT", "PUT_YOUR_TELEGRAM_TOKEN_HERE")

ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "347552741").split(",") if x.strip()]

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./nftbot.db")

COLLECTION_NAME = os.getenv("COLLECTION_NAME", "Field & Light")
COLLECTION_SYMBOL = os.getenv("COLLECTION_SYMBOL", "FLIGHT")
EDITION_SUPPLY = int(os.getenv("EDITION_SUPPLY", "200"))
PRICE_USDT = float(os.getenv("PRICE_USDT", "9.0"))
ROYALTY_PERCENT = float(os.getenv("ROYALTY_PERCENT", "6.0"))
PER_WALLET_LIMIT = int(os.getenv("PER_WALLET_LIMIT", "2"))

TON_RECEIVER_ADDRESS = os.getenv(
    "TON_RECEIVER_ADDRESS",
    "UQA98vnlaRu8CqlcAwxKRaa3kb2zJMMXF8euPneFhfZdPD2s"
)

TON_USD_RATE = float(os.getenv("TON_USD_RATE", "7.0"))
