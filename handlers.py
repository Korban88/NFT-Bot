from aiogram import types, Dispatcher
from config import (
    COLLECTION_NAME, COLLECTION_SYMBOL, EDITION_SUPPLY,
    PRICE_USDT, TON_USD_RATE, PER_WALLET_LIMIT, ROYALTY_PERCENT,
    TON_RECEIVER_ADDRESS, ADMIN_IDS
)
from db import SessionLocal, User, Order
from keyboards import main_kb, buy_inline

def _ton_equivalent(usdt: float) -> float:
    return usdt / TON_USD_RATE if TON_USD_RATE > 0 else 0.0

async def cmd_start(message: types.Message):
    with SessionLocal() as db:
        user = db.query(User).filter_by(tg_id=message.from_user.id).one_or_none()
        if not user:
            user = User(tg_id=message.from_user.id)
            db.add(user)
            db.commit()
    text = (
        f"Привет, {message.from_user.first_name or 'друг'}!\n\n"
        f"Это бот коллекции **{COLLECTION_NAME}** ({COLLECTION_SYMBOL}).\n"
        f"Тираж: {EDITION_SUPPLY} шт • Лимит: {PER_WALLET_LIMIT} на кошелёк • Роялти: {ROYALTY_PERCENT:.0f}%\n\n"
        f"Нажми «Купить NFT», чтобы увидеть цену и начать минт."
    )
    await message.answer(text, reply_markup=main_kb(), parse_mode="Markdown")

async def about_collection(message: types.Message):
    text = (
        f"**{COLLECTION_NAME}** — первая серия в концепции «Поле и свет».\n"
        f"Стандарт: 1155 • Тикер: {COLLECTION_SYMBOL}\n\n"
        "Iteration 0: минт с заглушкой оплаты. На следующем шаге добавим реальный non-custodial минт в TON "
        "и хранение медиа на IPFS."
    )
    await message.answer(text, parse_mode="Markdown")

async def my_profile(message: types.Message):
    with SessionLocal() as db:
        user = db.query(User).filter_by(tg_id=message.from_user.id).one_or_none()
        if not user:
            await message.answer("Профиль пуст. Нажми «Купить NFT», чтобы начать.")
            return
        orders = db.query(Order).filter_by(user_id=user.id).order_by(Order.id.desc()).all()
        if not orders:
            await message.answer("У тебя пока нет заказов.")
            return
        lines = ["Твои заказы:"]
        for o in orders[:10]:
            lines.append(f"#{o.id} • {o.status} • {o.qty} шт • {o.price_usdt:.2f} USDt (~{o.price_ton:.2f} TON)")
        await message.answer("\n".join(lines))

async def buy_nft(message: types.Message):
    price_ton = _ton_equivalent(PRICE_USDT)
    text = (
        "Витрина коллекции:\n\n"
        f"Цена: {PRICE_USDT:.2f} USDt (~{price_ton:.2f} TON)\n"
        f"Лимит: {PER_WALLET_LIMIT} шт на кошелёк\n"
        f"Получатель: `{TON_RECEIVER_ADDRESS}`\n\n"
        "Нажми кнопку ниже, чтобы создать заказ (Iteration 0: заглушка; оплата/подпись добавим дальше)."
    )
    await message.answer(text, reply_markup=buy_inline(PRICE_USDT, price_ton), parse_mode="Markdown")

async def cb_mint(call: types.CallbackQuery):
    with SessionLocal() as db:
        user = db.query(User).filter_by(tg_id=call.from_user.id).one_or_none()
        if not user:
            user = User(tg_id=call.from_user.id)
            db.add(user)
            db.commit()

        price_ton = _ton_equivalent(PRICE_USDT)
        order = Order(
            user_id=user.id,
            status="pending",
            price_usdt=PRICE_USDT,
            price_ton=price_ton,
            qty=1,
            note="Iteration 0 placeholder"
        )
        db.add(order)
        db.commit()
        order_id = order.id

    await call.message.edit_reply_markup()  # убираем кнопки
    await call.message.answer(
        "Заказ создан ✅\n"
        f"ID заказа: #{order_id}\n\n"
        "Сейчас это заглушка. На следующей итерации появится non-custodial подпись в кошельке и автоматическая проверка статуса.",
    )
    await call.answer()

async def admin_ping(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("Admin OK. Бот на связи.")

def register_handlers(dp: Dispatcher):
    dp.register_message_handler(cmd_start, commands=["start"])
    dp.register_message_handler(about_collection, lambda m: m.text and m.text.lower().startswith("о коллекции"))
    dp.register_message_handler(my_profile, lambda m: m.text and m.text.lower().startswith("мой профиль"))
    dp.register_message_handler(buy_nft, lambda m: m.text and m.text.lower().startswith("купить nft"))
    dp.register_callback_query_handler(cb_mint, text=["mint_usdt", "mint_ton"])
    dp.register_message_handler(admin_ping, commands=["admin_ping"])
