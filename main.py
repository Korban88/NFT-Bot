import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, executor
from config import settings
from handlers import register_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)
logger = logging.getLogger("nftbot")

def main():
    if not settings.BOT_TOKEN:
        logger.error("BOT_TOKEN is empty")
        sys.exit(1)

    bot = Bot(token=settings.BOT_TOKEN, parse_mode="HTML")
    dp = Dispatcher(bot)
    register_handlers(dp)

    logger.info("Starting NFT bot (Iteration 1 / Step 1)...")
    executor.start_polling(dp, skip_updates=True)

if __name__ == "__main__":
    if sys.platform != "win32":
        try:
            import uvloop
            uvloop.install()
        except Exception:
            pass
    main()
