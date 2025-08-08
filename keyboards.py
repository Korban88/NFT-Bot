from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

def main_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("Купить NFT"), KeyboardButton("О коллекции"))
    kb.add(KeyboardButton("Мой профиль"))
    return kb

def buy_inline(price_usdt: float, price_ton: float):
    ikb = InlineKeyboardMarkup(row_width=2)
    ikb.add(
        InlineKeyboardButton(text=f"Минт ({price_usdt:.2f} USDt)", callback_data="mint_usdt"),
        InlineKeyboardButton(text=f"Минт (~{price_ton:.2f} TON)", callback_data="mint_ton"),
    )
    return ikb
