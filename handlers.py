import logging
from aiogram import types, Dispatcher
from config import settings
from services.tonapi import TonAPI
from services.ipfs import PinataIPFS

logger = logging.getLogger("nftbot")

# ——— Команды ———

async def cmd_start(message: types.Message):
    text = (
        "NFT Бот запущен.\n\n"
        "Доступные команды:\n"
        "/scanner_on — включить мониторинг лотов\n"
        "/scanner_off — выключить мониторинг\n"
        "/scanner_settings — настройки фильтров\n"
        "/pay — ссылка на оплату (ton://transfer)\n"
        "/health — проверить TonAPI и Pinata\n"
    )
    await message.answer(text)

async def cmd_pay(message: types.Message):
    api = TonAPI()
    unique = api.unique_comment("pay")
    # На старте для теста ставим 0.1 TON. Потом заменим на динамическую сумму.
    link = api.build_ton_transfer_url(settings.TON_WALLET_ADDRESS, amount_ton=0.1, comment=unique)
    await api.close()
    await message.answer(
        "Оплата (тест 0.1 TON):\n"
        f"{link}\n\n"
        f"Комментарий-пометка: {unique}\n"
        "После оплаты мы сможем верифицировать транзакцию по комментарию."
    )

async def cmd_health(message: types.Message):
    # Проверяем доступ к TonAPI и Pinata
    ton_ok = "fail"
    pin_ok = "fail"

    try:
        ton = TonAPI()
        info = await ton.get_account_info(settings.TON_WALLET_ADDRESS)
        ton_ok = "ok" if info.get("address") else "warn"
        await ton.close()
    except Exception as e:
        logger.exception("TonAPI health error: %s", e)

    try:
        ipfs = PinataIPFS()
        cid = await ipfs.pin_json({"nftbot": "healthcheck"})
        url = ipfs.gateway_url(cid)
        pin_ok = "ok" if cid else "warn"
        await ipfs.close()
    except Exception as e:
        logger.exception("Pinata health error: %s", e)

    await message.answer(f"Health:\nTonAPI: {ton_ok}\nPinata: {pin_ok}")

async def cmd_scanner_on(message: types.Message):
    # Пока заглушка. На шаге 2 добавим планировщик и фильтры.
    await message.answer("Сканер включен (заглушка). В следующем шаге добавим реальные фильтры и уведомления.")

async def cmd_scanner_off(message: types.Message):
    await message.answer("Сканер выключен (заглушка).")

async def cmd_scanner_settings(message: types.Message):
    await message.answer(
        "Настройки сканера (заглушка):\n"
        "— скидка: ≥ 20–30%\n"
        "— фильтры: коллекции, цена, время, редкость\n"
        "В следующем шаге добавим сохранение в БД и изменение через кнопки."
    )

def register_handlers(dp: Dispatcher):
    dp.register_message_handler(cmd_start, commands=["start"])
    dp.register_message_handler(cmd_pay, commands=["pay"])
    dp.register_message_handler(cmd_health, commands=["health"])
    dp.register_message_handler(cmd_scanner_on, commands=["scanner_on"])
    dp.register_message_handler(cmd_scanner_off, commands=["scanner_off"])
    dp.register_message_handler(cmd_scanner_settings, commands=["scanner_settings"])
