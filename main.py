# main.py
import os
import asyncio
import logging

import asyncpg
from aiogram import Bot, Dispatcher, executor

from handlers import register_handlers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s"
)
log = logging.getLogger("nftbot")


def require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"ENV {name} is not set")
    return val


def main():
    # --- ENV ---
    bot_token = require_env("BOT_TOKEN")
    db_url = require_env("DATABASE_URL")

    # --- Aiogram ---
    bot = Bot(token=bot_token, parse_mode="HTML")
    dp = Dispatcher(bot)

    # --- Event loop / DB pool ---
    loop = asyncio.get_event_loop()
    pool = loop.run_until_complete(asyncpg.create_pool(dsn=db_url, min_size=1, max_size=5))
    log.info("✅ Подключение к базе данных успешно.")

    # --- Handlers ---
    register_handlers(dp, bot, pool)

    # --- Shutdown hook ---
    async def _on_shutdown(dp_: Dispatcher):
        try:
            await pool.close()
            log.info("DB pool closed.")
        except Exception as e:
            log.warning(f"DB pool close error: {e}")

    log.info("Starting NFT bot (Iteration 1 / Step 1)...")
    executor.start_polling(dp, skip_updates=True, on_shutdown=_on_shutdown)


if __name__ == "__main__":
    main()
