# handlers.py — минимально-рабочие хендлеры для aiogram v2
# Без внешних зависимостей: всё, что нужно, создаём здесь.

from aiogram import types, Dispatcher
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

# ==== Локальные клавиатуры (без отдельного файла) ====

def main_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("Купить NFT"), KeyboardButton("О коллекции"))
    kb.add(KeyboardButton("Мой профиль"))
    return kb

# ==== Хендлеры ====

async def cmd_start(message: types.Message):
    text = (
        "Привет! Это твой NFT-бот 👋\n\n"
        "Я пока в базовой конфигурации: показываю меню и отвечаю на основные кнопки.\n"
        "Нажми «Купить NFT», «О коллекции» или «Мой профиль»."
    )
    await message.answer(text, reply_markup=main_kb())

async def about_collection(message: types.Message):
    text = (
        "О коллекции:\n"
        "— Это демо-экран. На следующем шаге сюда подставим реальные данные коллекции и цены.\n"
        "— Добавим карточки NFT, превью и ссылки.\n"
    )
    await message.answer(text)

async def my_profile(message: types.Message):
    text = (
        "Мой профиль:\n"
        "— Здесь будет история твоих заявок/покупок.\n"
        "— После подключения базы появится список заказов и статусы."
    )
    await message.answer(text)

async def buy_nft(message: types.Message):
    text = (
        "Покупка NFT:\n"
        "— В Iteration 1 здесь появится реальная ссылка на оплату в TON (ton://transfer...).\n"
        "— После оплаты бот проверит транзакцию и вернёт ссылку на NFT/metadata.\n"
        "Сейчас это заглушка — проверяем, что бот отвечает на команды и кнопки."
    )
    await message.answer(text)

# ==== Регистрация хендлеров ====

def register_handlers(dp: Dispatcher):
    # Команда /start
    dp.register_message_handler(cmd_start, commands=["start"])

    # Текстовые кнопки (без нестандартных фильтров)
    dp.register_message_handler(
        buy_nft,
        lambda m: m.text and m.text.strip().lower().startswith("купить nft")
    )
    dp.register_message_handler(
        about_collection,
        lambda m: m.text and m.text.strip().lower().startswith("о коллекции")
    )
    dp.register_message_handler(
        my_profile,
        lambda m: m.text and m.text.strip().lower().startswith("мой профиль")
    )
# handlers.py - обработчики команд и платежей
