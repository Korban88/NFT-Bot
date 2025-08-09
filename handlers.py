import logging
from aiogram import types, Dispatcher
from aiogram.dispatcher.filters import Text
from config import settings
from services.tonapi import TonAPI
from services.ipfs import PinataIPFS

logger = logging.getLogger("nftbot")

# ---------- Клавиатура ----------
def build_main_kb() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, selective=True)
    kb.row(
        types.KeyboardButton("Купить NFT"),
        types.KeyboardButton("О коллекции"),
    )
    kb.row(types.KeyboardButton("Мой профиль"))
    return kb

# ---------- Команды ----------
async def cmd_start(message: types.Message):
    text = (
        "NFT Бот запущен.\n\n"
        "Доступные команды:\n"
        "/scanner_on — включить мониторинг лотов\n"
        "/scanner_off — выключить мониторинг\n"
        "/scanner_settings — настройки фильтров\n"
        "/pay — ссылка на оплату (ton://transfer)\n"
        "/verify <комментарий> — проверить оплату по комментарию\n"
        "/health — проверить TonAPI и Pinata\n"
    )
    await message.answer(text, reply_markup=build_main_kb())

async def cmd_pay(message: types.Message):
    try:
        api = TonAPI()
        unique = api.unique_comment("pay")
        link = api.build_ton_transfer_url(
            settings.TON_WALLET_ADDRESS, amount_ton=0.1, comment=unique
        )
        await api.close()
        await message.answer(
            "Оплата (тест 0.1 TON):\n"
            f"{link}\n\n"
            f"Комментарий-пометка: <code>{unique}</code>\n"
            "После оплаты пришли команду:\n"
            f"<code>/verify {unique}</code>"
        )
    except Exception as e:
        logger.exception("cmd_pay error: %s", e)
        await message.answer("Не удалось сформировать ссылку оплаты. Попробуй позже.")

async def cmd_verify(message: types.Message):
    """
    /verify <comment>
    Ищем входящую транзакцию с этим комментарием и суммой >= 0.1 TON.
    При успехе — пишем метаданные в IPFS (если настроен PINATA_JWT).
    """
    parts = (message.get_args() or "").strip()
    if not parts:
        await message.answer("Укажи комментарий: например\n<code>/verify pay-xxxxxx</code>")
        return

    comment = parts
    ton_ok = False
    tx_hash = None
    amount_ton = 0.0

    try:
        ton = TonAPI()
        found = await ton.find_payment_by_comment(
            settings.TON_WALLET_ADDRESS, comment_text=comment, min_amount_ton=0.1, limit=50
        )
        await ton.close()

        if not found:
            await message.answer("Платёж не найден. Проверь комментарий и сумму (не меньше 0.1 TON).")
            return

        tx, value_nano = found
        ton_ok = True
        tx_hash = tx.get("hash") or tx.get("transaction_id") or "unknown"
        amount_ton = value_nano / 1_000_000_000.0

    except Exception as e:
        logger.exception("verify TonAPI error: %s", e)
        await message.answer("Ошибка при обращении к TonAPI. Попробуй позже.")
        return

    # Если нашли платеж — пробуем создать метаданные NFT в IPFS
    try:
        ipfs = PinataIPFS()  # упадёт, если PINATA_JWT не задан
        name = "Field & Light — Early Access"
        description = "Покупка зафиксирована в блокчейне TON. Метаданные хранятся в IPFS."
        # Пока картинку-заглушку не грузим — подставим ссылку-плейсхолдер (можно заменить позже на IPFS CID картинки)
        image_url = "https://gateway.pinata.cloud/ipfs/QmPlaceholderImage"  # заменим на реальный CID позже
        attributes = {
            "collection": "FLIGHT",
            "buyer_telegram_id": message.from_user.id,
            "tx_hash": tx_hash,
            "amount_ton": round(amount_ton, 6),
            "comment": comment,
        }
        cid, url = await ipfs.pin_nft_metadata(name, description, image_url, attributes)
        await ipfs.close()

        await message.answer(
            "Оплата подтверждена ✅\n"
            f"Сумма: <b>{amount_ton:.3f} TON</b>\n"
            f"TX: <code>{tx_hash}</code>\n\n"
            "Метаданные NFT сохранены в IPFS:\n"
            f"CID: <code>{cid}</code>\n"
            f"URL: {url}"
        )
    except Exception as e:
        # Если Pinata не настроен — всё равно подтверждаем платеж, но сообщаем, что мету не записали
        logger.exception("Pinata error: %s", e)
        if ton_ok:
            await message.answer(
                "Оплата подтверждена ✅\n"
                f"Сумма: <b>{amount_ton:.3f} TON</b>\n"
                f"TX: <code>{tx_hash}</code>\n\n"
                "Но IPFS (Pinata) не настроен, метаданные не записаны. "
                "Добавь PINATA_JWT в Railway → Variables и повтори /verify."
            )
        else:
            await message.answer("Не удалось подтвердить оплату.")

async def cmd_health(message: types.Message):
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
        _ = ipfs.gateway_url(cid)
        pin_ok = "ok" if cid else "warn"
        await ipfs.close()
    except Exception as e:
        logger.exception("Pinata health error: %s", e)

    await message.answer(f"Health:\nTonAPI: {ton_ok}\nPinata: {pin_ok}")

async def cmd_scanner_on(message: types.Message):
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

# ---------- Обработчики кнопок (reply-клавиатура) ----------
async def on_buy_nft(message: types.Message):
    await message.answer(
        "Шаг покупки:\n1) Нажми /pay — получишь ссылку на перевод и уникальный комментарий.\n"
        "2) Соверши перевод.\n3) Пришли команду /verify <комментарий>.\n"
        "При успехе метаданные будут сохранены в IPFS."
    )

async def on_about_collection(message: types.Message):
    await message.answer("Field & Light (FLIGHT): коллекция в разработке. Добавим описание и ссылки на маркетплейсы.")

async def on_profile(message: types.Message):
    await message.answer("Профиль скоро подключим: баланс, покупки, привязка кошелька.")

def register_handlers(dp: Dispatcher):
    # Команды
    dp.register_message_handler(cmd_start, commands=["start"])
    dp.register_message_handler(cmd_pay, commands=["pay"])
    dp.register_message_handler(cmd_verify, commands=["verify"])
    dp.register_message_handler(cmd_health, commands=["health"])
    dp.register_message_handler(cmd_scanner_on, commands=["scanner_on"])
    dp.register_message_handler(cmd_scanner_off, commands=["scanner_off"])
    dp.register_message_handler(cmd_scanner_settings, commands=["scanner_settings"])

    # Кнопки (по тексту)
    dp.register_message_handler(on_buy_nft, Text(equals="Купить NFT"))
    dp.register_message_handler(on_about_collection, Text(equals="О коллекции"))
    dp.register_message_handler(on_profile, Text(equals="Мой профиль"))
