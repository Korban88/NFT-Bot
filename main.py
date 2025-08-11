# main.py
import os
import asyncio
import logging

import asyncpg
from aiogram import Bot, Dispatcher, executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage

from config import settings
from db import get_pool, init_db
from handlers import register_handlers, scanner_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s"
)
log = logging.getLogger("nftbot")

def require_env(name: str):
    val = os.getenv(name) or ""
    if not val:
        raise RuntimeError(f"{name} is empty")
    return val

def main():
    require_env("BOT_TOKEN")
    require_env("DATABASE_URL")

    bot = Bot(token=settings.BOT_TOKEN, parse_mode="HTML")
    dp = Dispatcher(bot, storage=MemoryStorage())

    loop = asyncio.get_event_loop()

    async def _on_startup(dp_: Dispatcher):
        pool = await get_pool()
        await init_db()
        # регистрируем handlers уже зная pool/bot
        register_handlers(dp, bot, pool)
        # запускаем фоновый сканер
        loop.create_task(scanner_loop(bot, pool))
        log.info("Scanner loop started.")

    async def _on_shutdown(dp_: Dispatcher):
        try:
            pool = await get_pool()
            await pool.close()
            log.info("DB pool closed.")
        except Exception as e:
            log.warning(f"DB pool close error: {e}")

    log.info("Starting NFT bot (Scanner v1)...")
    executor.start_polling(dp, skip_updates=True, on_startup=_on_startup, on_shutdown=_on_shutdown)

if __name__ == "__main__":
    main()
