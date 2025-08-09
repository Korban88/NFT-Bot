# main.py — старт NFT бота (Iteration 1) с авто-сбросом вебхука
import logging
import asyncio
from aiogram import Bot, Dispatcher, executor
from config import TELEGRAM_TOKEN
from db import init_db
from handlers import register_handlers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nftbot")

async def _prepare(bot: Bot):
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook removed (drop_pending_updates=True).")
    except Exception as e:
        logger.warning(f"Failed to delete webhook: {e}")

def main():
    init_db()
    bot = Bot(token=TELEGRAM_TOKEN, parse_mode="HTML")
    dp = Dispatcher(bot)
    register_handlers(dp)
    logger.info("Starting NFT bot (Iteration 1)...")

    loop = asyncio.get_event_loop()
    loop.run_until_complete(_prepare(bot))

    executor.start_polling(dp, skip_updates=True)

if __name__ == "__main__":
    main()
