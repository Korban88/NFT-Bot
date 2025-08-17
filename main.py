# main.py
import asyncio
import logging

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.utils import executor

from config import settings
from handlers import register_handlers            # хэндлеры остаются тут
from scanner import scanner_loop                  # сканер вынесен в scanner.py

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nftbot")


async def on_startup(dp: Dispatcher):
    logger.info("Starting NFT bot (Iteration 1 / Step 1)...")
    # Фоновый цикл сканера
    dp.loop.create_task(scanner_loop())


def main():
    bot = Bot(token=settings.BOT_TOKEN, parse_mode=types.ParseMode.HTML)
    dp = Dispatcher(bot, storage=MemoryStorage())

    register_handlers(dp)

    # Поллинг
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)


if __name__ == "__main__":
    main()
