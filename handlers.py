# handlers.py
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from aiogram import Dispatcher, types
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from db import (
    get_or_create_scanner_settings,  # (user_id)
    get_wallet,                      # ()
    set_wallet,                      # (user_id, address)
    update_scanner_settings,         # (user_id, **kwargs)
    set_scanner_enabled,             # (user_id, enabled)
)

# ------------------------------------------------------------
# Вспомогательные утилиты форматирования
# ------------------------------------------------------------

def _main_reply_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("🏁 Старт"))
    kb.add(KeyboardButton("👛 Кошелёк"), KeyboardButton("🛠 Настройки сканера"))
    kb.add(KeyboardButton("▶️ Включить сканер"), KeyboardButton("⏸ Выключить сканер"))
    kb.add(KeyboardButton("ℹ️ Статус"))
    return kb


def _format_scanner_settings(st: Dict[str, Any]) -> str:
    def fmt_ton(v: Optional[Decimal]) -> str:
        if v is None:
            return "нет"
        try:
            return f"{float(v):.3f} TON"
        except Exception:
            return str(v)

    parts = [
        f"Сканер: {'включен' if st.get('enabled') or st.get('scanner_enabled') else 'выключен'}",
        f"Скидка (мин): {float(st.get('min_discount_pct') or st.get('min_discount') or 0):.0f} %",
        f"Цена (мин): {fmt_ton(Decimal(str(st.get('min_price_ton'))) if st.get('min_price_ton') is not None else None)}",
        f"Цена (макс): {fmt_ton(Decimal(str(st.get('max_price_ton'))) if st.get('max_price_ton') is not None else None)}",
        f"Коллекции: {', '.join(st.get('collections') or []) if st.get('collections') else 'все'}",
        f"Период обновления: {int(st.get('poll_seconds') or 60)}s",
    ]
    return "\n".join(parts)


async def _ensure_user_settings(user_id: int) -> Dict[str, Any]:
    st = await get_or_create_scanner_settings(user_id)
    # Значения по умолчанию / приведение ключей к ожидаемым
    st.setdefault("enabled", False)
    st.setdefault("min_discount_pct", st.get("min_discount", 20))
    st.setdefault("min_price_ton", None)
    st.setdefault("max_price_ton", None)
    st.setdefault("collections", [])
    st.setdefault("poll_seconds", 60)
    return st


# ------------------------------------------------------------
# Команды и хэндлеры
# ------------------------------------------------------------

async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я NFT-бот. Слежу за выгодными лотами на TON-маркетах и могу показать твою коллекцию.\n"
        "Используй кнопки ниже.",
        reply_markup=_main_reply_kb(),
    )


async def cmd_status(message: types.Message):
    user_id = message.from_user.id
    st = await _ensure_user_settings(user_id)
    wallet = await get_wallet()
    wallet_str = wallet or "не привязан"
    text = (
        f"👤 Пользователь: {user_id}\n"
        f"👛 Кошелёк: <code>{wallet_str}</code>\n\n"
        f"🛠 Текущие настройки сканера:\n{_format_scanner_settings(st)}"
    )
    await message.answer(text, reply_markup=_main_reply_kb())


async def cmd_wallet(message: types.Message):
    wallet = await get_wallet()
    if wallet:
        await message.answer(
            f"Текущий TON-адрес: <code>{wallet}</code>\n"
            f"Чтобы заменить — отправь новый адрес одним сообщением.",
            reply_markup=_main_reply_kb(),
        )
    else:
        await message.answer(
            "Кошелёк ещё не привязан. Отправь TON-адрес одним сообщением, я сохраню.",
            reply_markup=_main_reply_kb(),
        )


async def on_plain_address(message: types.Message):
    # Простая эвристика валидации TON-адреса (friendly/raw)
    text = (message.text or "").strip()
    if len(text) < 48 or len(text) > 80:
        return
    if not any(ch.isalnum() for ch in text):
        return
    await set_wallet(message.from_user.id, text)
    await message.answer(
        f"Сохранил TON-адрес: <code>{text}</code>",
        reply_markup=_main_reply_kb(),
    )


async def cmd_scanner_settings(message: types.Message):
    user_id = message.from_user.id
    st = await _ensure_user_settings(user_id)

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("Мин. скидка −5%", callback_data="min_disc:-5"),
        InlineKeyboardButton("Мин. скидка +5%", callback_data="min_disc:+5"),
    )
    kb.add(
        InlineKeyboardButton("Мин. цена −0.5 TON", callback_data="min_price:-0.5"),
        InlineKeyboardButton("Мин. цена +0.5 TON", callback_data="min_price:+0.5"),
    )
    kb.add(
        InlineKeyboardButton("Макс. цена −0.5 TON", callback_data="max_price:-0.5"),
        InlineKeyboardButton("Макс. цена +0.5 TON", callback_data="max_price:+0.5"),
    )
    kb.add(
        InlineKeyboardButton("Интервал −10s", callback_data="poll:-10"),
        InlineKeyboardButton("Интервал +10s", callback_data="poll:+10"),
    )
    kb.add(InlineKeyboardButton("Очистить коллекции", callback_data="cols:clear"))

    await message.answer(
        "🛠 Настройки сканера:\n"
        + _format_scanner_settings(st)
        + "\n\nПодправь параметры кнопками ниже.",
        reply_markup=kb,
    )


async def cb_settings(call: types.CallbackQuery):
    user_id = call.from_user.id
    st = await _ensure_user_settings(user_id)
    data = call.data or ""

    try:
        if data.startswith("min_disc:"):
            delta = int(data.split(":", 1)[1])
            cur = int(st.get("min_discount_pct") or 0)
            cur = max(0, min(90, cur + delta))
            st["min_discount_pct"] = cur
            await update_scanner_settings(user_id, min_discount=cur)

        elif data.startswith("min_price:"):
            delta = Decimal(data.split(":", 1)[1])
            cur_raw = st.get("min_price_ton")
            cur = Decimal(str(cur_raw)) if cur_raw is not None else Decimal("0")
            cur = max(Decimal("0"), cur + delta)
            st["min_price_ton"] = str(cur)
            await update_scanner_settings(user_id, min_price_ton=str(cur))

        elif data.startswith("max_price:"):
            delta = Decimal(data.split(":", 1)[1])
            cur_raw = st.get("max_price_ton")
            cur = Decimal(str(cur_raw)) if cur_raw is not None else Decimal("0")
            cur = max(Decimal("0"), cur + delta)
            st["max_price_ton"] = str(cur)
            await update_scanner_settings(user_id, max_price_ton=str(cur))

        elif data.startswith("poll:"):
            delta = int(data.split(":", 1)[1])
            cur = int(st.get("poll_seconds") or 60)
            cur = max(10, min(3600, cur + delta))
            st["poll_seconds"] = cur
            await update_scanner_settings(user_id, poll_seconds=cur)

        elif data == "cols:clear":
            st["collections"] = []
            await update_scanner_settings(user_id, collections=[])

        await call.answer("Обновлено")
        await call.message.edit_text(
            "🛠 Настройки сканера обновлены:\n" + _format_scanner_settings(st),
            reply_markup=call.message.reply_markup,
        )
    except InvalidOperation:
        await call.answer("Некорректное число", show_alert=True)


async def cmd_scanner_on(message: types.Message):
    user_id = message.from_user.id
    await set_scanner_enabled(user_id, True)
    await message.answer("Сканер включен ✅", reply_markup=_main_reply_kb())


async def cmd_scanner_off(message: types.Message):
    user_id = message.from_user.id
    await set_scanner_enabled(user_id, False)
    await message.answer("Сканер выключен ⏸", reply_markup=_main_reply_kb())


# ------------------------------------------------------------
# Регистрация хэндлеров
# ------------------------------------------------------------

def register_handlers(dp: Dispatcher) -> None:
    dp.register_message_handler(cmd_start, commands={"start"})
    dp.register_message_handler(cmd_status, lambda m: m.text == "ℹ️ Статус")
    dp.register_message_handler(cmd_wallet, lambda m: m.text == "👛 Кошелёк")
    dp.register_message_handler(cmd_scanner_settings, lambda m: m.text == "🛠 Настройки сканера")
    dp.register_message_handler(cmd_scanner_on, lambda m: m.text == "▶️ Включить сканер")
    dp.register_message_handler(cmd_scanner_off, lambda m: m.text == "⏸ Выключить сканер")
    dp.register_message_handler(cmd_start, lambda m: m.text == "🏁 Старт")
    dp.register_message_handler(on_plain_address, content_types=types.ContentTypes.TEXT)
    dp.register_callback_query_handler(cb_settings, lambda c: (c.data or "").split(":")[0] in {
        "min_disc", "min_price", "max_price", "poll", "cols"
    })
