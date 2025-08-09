import logging
from aiogram import types, Dispatcher
from aiogram.dispatcher.filters import Text

from config import settings
from services.tonapi import TonAPI
from services.ipfs import PinataIPFS
from db import record_payment, user_stats, user_last_payments

logger = logging.getLogger("nftbot")


# ---------- Клавиатура ----------
def build_main_kb() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, selective=True)
    kb.row(types.KeyboardButton("Купить NFT"), types.KeyboardButton("О коллекции"))
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
        "/verify &lt;комментарий&gt; — проверить оплату по комментарию\n"
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
            f"Комментарий‑пометка: <code>{unique}</code>\n"
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
    При успехе — пишем метаданные в IPFS (если настроен PINATA_JWT) и сохраняем запись в БД.
    """
    comment = (message.get_args() or "").strip()
    if not comment:
        await message.answer("Укажи комментарий, например: <code>/verify pay-xxxxxx</code>")
        return

    # 1) Ищем платёж в TON
    try:
        ton = TonAPI()
        found = await ton.find_payment_by_comment(
            settings.TON_WALLET_ADDRESS, comment_text=comment, min_amount_ton=0.1, limit=50
        )
        await ton.close()
    except Exception as e:
        logger.exception("verify TonAPI error: %s", e)
        await message.answer("Ошибка при обращении к TonAPI. Попробуй позже.")
        return

    if not found:
        await message.answer("Платёж не найден. Проверь комментарий и сумму (не меньше 0.1 TON).")
        return

    tx, value_nano = found
    tx_hash = tx.get("hash") or tx.get("transaction_id") or "unknown"
    amount_ton = value_nano / 1_000_000_000.0

    # 2) Пытаемся записать метаданные в IPFS (Pinata)
    cid = None
    url = None
    try:
        ipfs = PinataIPFS()
        name = "Field & Light — Early Access"
        description = "Покупка зафиксирована в блокчейне TON. Метаданные хранятся в IPFS."
        image_url = "https://gateway.pinata.cloud/ipfs/QmPlaceholderImage"  # заменим позже
        attributes = {
            "collection": "FLIGHT",
            "buyer_telegram_id": message.from_user.id,
            "tx_hash": tx_hash,
            "amount_ton": round(amount_ton, 6),
            "comment": comment,
        }
        cid, url = await ipfs.pin_nft_metadata(name, description, image_url, attributes)
        await ipfs.close()
    except Exception as e:
        logger.exception("Pinata error: %s", e)

    # 3) Сохраняем запись в БД (даже если Pinata не записалась)
    try:
        await record_payment(
            user_id=message.from_user.id,
            tx_hash=tx_hash,
            comment=comment,
            amount_nano=int(value_nano),
            amount_ton=amount_ton,
            cid=cid,
            url=url,
        )
    except Exception as e:
        logger.exception("DB record_payment error: %s", e)

    # 4) Ответ пользователю
    if cid and url:
        await message.answer(
            "Оплата подтверждена ✅\n"
            f"Сумма: <b>{amount_ton:.3f} TON</b>\n"
            f"TX: <code>{tx_hash}</code>\n\n"
            "Метаданные NFT сохранены в IPFS:\n"
            f"CID: <code>{cid}</code>\n"
            f"URL: {url}"
        )
    else:
        await message.answer(
            "Оплата подтверждена ✅\n"
            f"Сумма: <b>{amount_ton:.3f} TON</b>\n"
            f"TX: <code>{tx_hash}</code>\n\n"
            "IPFS (Pinata) не настроен или временно недоступен — метаданные не записаны.\n"
            "Запись об оплате сохранена."
        )


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


# ---------- Обработчики кнопок ----------
async def on_buy_nft(message: types.Message):
    await message.answer(
        "Шаг покупки:\n"
        "1) Нажми /pay — получишь ссылку на перевод и уникальный комментарий.\n"
        "2) Соверши перевод.\n"
        "3) Пришли команду: <code>/verify твой_комментарий</code>.\n"
        "При успехе метаданные будут сохранены в IPFS."
    )


async def on_about_collection(message: types.Message):
    await message.answer("Field & Light (FLIGHT): коллекция в разработке. Добавим описание и ссылки на маркетплейсы.")


async def on_profile(message: types.Message):
    try:
        stats = await user_stats(message.from_user.id)
        rows = await user_last_payments(message.from_user.id, limit=3)
        lines = [
            f"Покупок: <b>{stats['count']}</b>",
            f"Суммарно: <b>{stats['sum_ton']:.3f} TON</b>",
            "",
            "Последние сделки:",
        ]
        if not rows:
            lines.append("— пока пусто")
        else:
            for r in rows:
                part = (
                    f"• {r['created_at'].strftime('%Y-%m-%d %H:%M')} — {r['amount_ton']:.3f} TON, "
                    f"tx <code>{(r['tx_hash'] or '')[:12]}...</code>"
                )
                if r["cid"]:
                    part += f" — <a href='{r['url']}'>IPFS</a>"
                lines.append(part)
        await message.answer("\n".join(lines), disable_web_page_preview=True)
    except Exception as e:
        logger.exception("profile error: %s", e)
        await message.answer("Профиль временно недоступен, попробуй позже.")


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
