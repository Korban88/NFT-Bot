# config.py — настройки бота

import os
from dotenv import load_dotenv

# Загружаем .env, если запускаем локально
load_dotenv()

# Читаем токен из переменных окружения (Railway или .env)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN_NFTBOT")

if not TELEGRAM_TOKEN:
    raise ValueError("Переменная окружения TELEGRAM_TOKEN_NFTBOT не задана!")
# config.py - конфигурация бота
