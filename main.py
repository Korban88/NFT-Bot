import logging
from aiogram import Bot, Dispatcher, executor
from config import TELEGRAM_TOKEN
from db import init_db
from handlers import register_handlers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nftbot")

def main():
    init_db()
    bot = Bot(token=TELEGRAM_TOKEN, parse_mode="HTML")
    dp = Dispatcher(bot)
    register_handlers(dp)
    logger.info("Starting NFT bot (Iteration 0)...")
    executor.start_polling(dp, skip_updates=True)

if __name__ == "__main__":
    main()
