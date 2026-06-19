"""
Telegram-бот для выдачи прокси IPRoyal (residential) по странам
+ админка: добавление/удаление доступа в реальном времени (без перезапуска).

КАК ЭТО РАБОТАЕТ
  IPRoyal residential использует один общий шлюз (geo.iproyal.com:12321),
  а страна и сессия задаются ДОПОЛНЕНИЯМИ к паролю:
      пароль_country-gr_session-XXXX_lifetime-1h
  Боту не нужен список прокси — он сам собирает нужную строку под страну.

ДОСТУП
  - Админы (ADMIN_USERNAMES / ADMIN_IDS) — всегда имеют доступ и видят кнопки
    управления: «Добавить доступ», «Убрать доступ», «Список».
  - Обычные пользователи добавляются админами через кнопку и хранятся в access.json.

ЗАПУСК
  pip install aiogram
  python iproyal_proxy_bot.py
"""

import asyncio
import json
import os
import secrets
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# ─────────────────────────────  НАСТРОЙКИ  ─────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")  # задаётся в «Переменных окружения» на хостинге

# Хост/порт не секретны и заданы прямо тут. Логин/пароль берутся из
# переменных окружения хостинга (PROXY_USERNAME / PROXY_PASSWORD).
PROXY_HOST = "geo.iproyal.com"
PROXY_PORT = "12321"
PROXY_USERNAME = os.environ.get("PROXY_USERNAME", "")
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD", "")

# "Прилипшая" сессия: один IP держится указанное время (10m, 30m, 1h, 24h ... до 7d).
# Поставь "" — будет ротация (новый IP на каждый запрос).
SESSION_LIFETIME = "1h"

# Админы — всегда имеют доступ и могут управлять списком (через кнопки).
# Ники без @ и маленькими буквами (Telegram их не различает по регистру).
ADMIN_USERNAMES: set[str] = {
    "bepowell",
    "trendbee",
    "patumkin",
    "ashlieq",
    "luparafuck",
}
ADMIN_IDS: set[int] = set()   # можно добавить числовые id админов, если нужно

# Файл, куда сохраняется выданный доступ (чтобы переживал перезапуск бота).
ACCESS_FILE = Path(__file__).with_name("access.json")

# ─────────────────────────────  СТРАНЫ  ─────────────────────────────

COUNTRIES = {
    "gr": ("🇬🇷", "Греция"),
    "nl": ("🇳🇱", "Нидерланды"),
    "es": ("🇪🇸", "Испания"),
    "be": ("🇧🇪", "Бельгия"),
    "fr": ("🇫🇷", "Франция"),
}

if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN. Добавь переменную окружения BOT_TOKEN на хостинге.")
if not PROXY_USERNAME or not PROXY_PASSWORD:
    print("ВНИМАНИЕ: PROXY_USERNAME/PROXY_PASSWORD не заданы — прокси работать не будут.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class AdminFlow(StatesGroup):
    add = State()
    remove = State()


# ─────────────────────────  ДОСТУП (загрузка/сохранение)  ─────────────────────────

def load_access() -> dict:
    if ACCESS_FILE.exists():
        try:
            data = json.loads(ACCESS_FILE.read_text(encoding="utf-8"))
            return {
                "usernames": set(data.get("usernames", [])),
                "ids": set(int(i) for i in data.get("ids", [])),
            }
        except Exception:
            pass
    return {"usernames": set(), "ids": set()}


def save_access() -> None:
    ACCESS_FILE.write_text(
        json.dumps(
            {"usernames": sorted(access["usernames"]), "ids": sorted(access["ids"])},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


access = load_access()


def is_admin(user) -> bool:
    if user.id in ADMIN_IDS:
        return True
    if user.username and user.username.lower() in ADMIN_USERNAMES:
        return True
    return False


def is_allowed(user) -> bool:
    if is_admin(user):
        return True
    if user.id in access["ids"]:
        return True
    if user.username and user.username.lower() in access["usernames"]:
        return True
    return False


def parse_target(text: str):
    """'123456' -> ('id', 123456);  '@vasya'/'vasya' -> ('username', 'vasya');  иначе None."""
    text = (text or "").strip()
    if not text or text.startswith("/"):
        return None
    if text.lstrip("-").isdigit():
        return ("id", int(text))
    uname = text.lstrip("@").lower()
    return ("username", uname) if uname else None


def access_list_text() -> str:
    admins = ", ".join("@" + u for u in sorted(ADMIN_USERNAMES))
    unames = ", ".join("@" + u for u in sorted(access["usernames"])) or "—"
    ids = ", ".join(str(i) for i in sorted(access["ids"])) or "—"
    return (
        f"<b>👑 Админы:</b> {admins}\n\n"
        f"<b>✅ Доступ по нику:</b> {unames}\n"
        f"<b>✅ Доступ по id:</b> {ids}"
    )


# ─────────────────────────────  КЛАВИАТУРЫ  ─────────────────────────────

def main_keyboard(user) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{flag} {name}", callback_data=f"country:{code}")]
        for code, (flag, name) in COUNTRIES.items()
    ]
    if is_admin(user):
        rows.append([
            InlineKeyboardButton(text="➕ Добавить доступ", callback_data="admin:add"),
            InlineKeyboardButton(text="➖ Убрать доступ", callback_data="admin:remove"),
        ])
        rows.append([InlineKeyboardButton(text="👥 Список доступа", callback_data="admin:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="admin:cancel")]
    ])


# ─────────────────────────────  ПРОКСИ  ─────────────────────────────

def build_proxy(code: str) -> str:
    session = secrets.token_hex(4)  # уникальный ID сессии → отдельный IP
    password = f"{PROXY_PASSWORD}_country-{code}_session-{session}"
    if SESSION_LIFETIME:
        password += f"_lifetime-{SESSION_LIFETIME}"
    return f"{PROXY_HOST}:{PROXY_PORT}:{PROXY_USERNAME}:{password}"


def proxy_message(code: str, user) -> tuple[str, InlineKeyboardMarkup]:
    flag, name = COUNTRIES[code]
    line = build_proxy(code)
    host, port, usr, pwd = line.split(":", 3)
    url = f"http://{usr}:{pwd}@{host}:{port}"
    text = (
        f"{flag} <b>{name}</b>\n\n"
        f"<b>HOST:PORT:USER:PASS</b>\n<code>{line}</code>\n\n"
        f"<b>URL</b>\n<code>{url}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Другой IP", callback_data=f"country:{code}")],
        [InlineKeyboardButton(text="⬅️ К списку стран", callback_data="back")],
    ])
    return text, kb


# ─────────────────────────────  ХЭНДЛЕРЫ  ─────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_allowed(message.from_user):
        await message.answer("⛔ Доступ к боту ограничен.")
        return
    await message.answer("Выбери страну прокси:", reply_markup=main_keyboard(message.from_user))


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_keyboard(message.from_user))


@dp.callback_query(F.data == "back")
async def cb_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "Выбери страну прокси:", reply_markup=main_keyboard(callback.from_user)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("country:"))
async def cb_country(callback: CallbackQuery):
    if not is_allowed(callback.from_user):
        await callback.answer("⛔ Доступ ограничен.", show_alert=True)
        return
    code = callback.data.split(":", 1)[1]
    if code not in COUNTRIES:
        await callback.answer("Неизвестная страна.", show_alert=True)
        return
    text, kb = proxy_message(code, callback.from_user)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Готово ✅")


@dp.callback_query(F.data == "admin:cancel")
async def cb_admin_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "Выбери страну прокси:", reply_markup=main_keyboard(callback.from_user)
    )
    await callback.answer("Отменено")


@dp.callback_query(F.data == "admin:list")
async def cb_admin_list(callback: CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Только для админов.", show_alert=True)
        return
    await callback.message.answer(access_list_text(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin:add")
async def cb_admin_add(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Только для админов.", show_alert=True)
        return
    await state.set_state(AdminFlow.add)
    await callback.message.answer(
        "Кому дать доступ? Пришли <b>id</b> или <b>@username</b> одним сообщением.",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:remove")
async def cb_admin_remove(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("⛔ Только для админов.", show_alert=True)
        return
    await state.set_state(AdminFlow.remove)
    await callback.message.answer(
        "У кого убрать доступ? Пришли <b>id</b> или <b>@username</b> одним сообщением.",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@dp.message(AdminFlow.add)
async def process_add(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        await state.clear()
        return
    target = parse_target(message.text)
    if target is None:
        await message.answer("Не понял. Пришли id (число) или @username, либо нажми «Отмена».")
        return
    kind, value = target
    if kind == "id":
        access["ids"].add(value)
        label = str(value)
    else:
        access["usernames"].add(value)
        label = "@" + value
    save_access()
    await state.clear()
    await message.answer(f"✅ Доступ выдан: {label}", reply_markup=main_keyboard(message.from_user))


@dp.message(AdminFlow.remove)
async def process_remove(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        await state.clear()
        return
    target = parse_target(message.text)
    if target is None:
        await message.answer("Не понял. Пришли id (число) или @username, либо нажми «Отмена».")
        return
    kind, value = target
    if kind == "id":
        existed = value in access["ids"]
        access["ids"].discard(value)
        label = str(value)
    else:
        existed = value in access["usernames"]
        access["usernames"].discard(value)
        label = "@" + value
    save_access()
    await state.clear()
    msg = f"🗑 Доступ убран: {label}" if existed else f"ℹ️ {label} и так не было в списке."
    await message.answer(msg, reply_markup=main_keyboard(message.from_user))


async def main():
    print("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())