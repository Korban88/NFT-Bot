import logging
from contextlib import asynccontextmanager

logger = logging.getLogger("nftbot")

@asynccontextmanager
async def lifespan(*resources):
    try:
        yield
    finally:
        for r in resources:
            try:
                close = getattr(r, "close", None)
                if callable(close):
                    await close()
            except Exception as e:
                logger.exception("Error on resource close: %s", e)
