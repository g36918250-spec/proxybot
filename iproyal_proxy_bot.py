"""
Telegram-бот для выдачи прокси IPRoyal (residential) по странам.
Страны: Бельгия, Нидерланды, Греция.
"""

import asyncio
import os
import secrets
import json
import requests as req_lib
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ─────────────────────────────  НАСТРОЙКИ  ─────────────────────────────

BOT_TOKEN      = os.environ["BOT_TOKEN"]
PROXY_HOST     = "geo.iproyal.com"
PROXY_PORT     = "12321"
PROXY_USERNAME = os.environ["PROXY_USERNAME"]
PROXY_PASSWORD = os.environ["PROXY_PASSWORD"]
SESSION_LIFETIME = "1h"

# Файл доступа — хранится рядом со скриптом, переживает перезапуски бота.
# При необходимости переопределяй через переменную окружения ACCESS_FILE_PATH.
ACCESS_FILE = Path(os.environ.get("ACCESS_FILE_PATH", str(Path(__file__).with_name("access.json"))))

# Админы — зашиты в код, не слетают при перезаливке. Всегда имеют доступ + управление.
ADMIN_USERNAMES: set[str] = {
    "bepowell",
    "trendbee",
    "patumkin",
    "ashlieq",
    "luparafuck",
}
ADMIN_IDS: set[int] = set()

# ─────────────────────────────  СТРАНЫ  ─────────────────────────────

COUNTRIES = {
    "be": ("🇧🇪", "Бельгия"),
    "nl": ("🇳🇱", "Нидерланды"),
    "gr": ("🇬🇷", "Греция"),
}

# ─────────────────────────────  ДОСТУП  ─────────────────────────────

def load_access() -> dict:
    try:
        if ACCESS_FILE.exists():
            return json.loads(ACCESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"ids": [], "usernames": []}


def save_access(data: dict) -> None:
    try:
        ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACCESS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[WARN] Не удалось сохранить access.json: {e}")


def is_admin(user) -> bool:
    if user.id in ADMIN_IDS:
        return True
    if user.username and user.username.lower() in ADMIN_USERNAMES:
        return True
    return False


def is_allowed(user) -> bool:
    if is_admin(user):
        return True
    data = load_access()
    if user.id in data.get("ids", []):
        return True
    if user.username and user.username.lower() in [u.lower() for u in data.get("usernames", [])]:
        return True
    return False

# ─────────────────────────────  ПРОКСИ  ─────────────────────────────

def build_proxy_string(code: str) -> str:
    session  = secrets.token_hex(4)
    password = f"{PROXY_PASSWORD}_country-{code}_session-{session}"
    if SESSION_LIFETIME:
        password += f"_lifetime-{SESSION_LIFETIME}"
    return f"{PROXY_HOST}:{PROXY_PORT}:{PROXY_USERNAME}:{password}"


def check_proxy(proxy_str: str) -> tuple[bool, str]:
    """Проверяет прокси реальным запросом. Возвращает (живой, внешний_IP)."""
    host, port, user, pwd = proxy_str.split(":", 3)
    proxy_url = f"http://{user}:{pwd}@{host}:{port}"
    proxies   = {"http": proxy_url, "https": proxy_url}
    try:
        r  = req_lib.get("https://api.ipify.org?format=json", proxies=proxies, timeout=12)
        ip = r.json().get("ip", "?")
        return True, ip
    except Exception:
        return False, ""

# ─────────────────────────────  КЛАВИАТУРЫ  ─────────────────────────────

def countries_keyboard(admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{flag} {name}", callback_data=f"country:{code}")]
        for code, (flag, name) in COUNTRIES.items()
    ]
    if admin:
        rows += [
            [InlineKeyboardButton(text="➕ Добавить доступ", callback_data="add_access")],
            [InlineKeyboardButton(text="➖ Убрать доступ",  callback_data="remove_access")],
            [InlineKeyboardButton(text="👥 Список доступа", callback_data="list_access")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="back")]
    ])


def proxy_result_kb(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Другой IP",         callback_data=f"country:{code}")],
        [InlineKeyboardButton(text="⬅️ К списку стран", callback_data="back")],
    ])

# ─────────────────────────────  FSM  ─────────────────────────────

class AdminFlow(StatesGroup):
    waiting_add    = State()
    waiting_remove = State()

# ─────────────────────────────  БОТ  ─────────────────────────────

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not is_allowed(message.from_user):
        await message.answer("⛔ Доступ к боту ограничен.")
        return
    await message.answer("Выбери страну прокси:", reply_markup=countries_keyboard(is_admin(message.from_user)))


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=countries_keyboard(is_admin(message.from_user)))


@dp.callback_query(F.data == "back")
async def cb_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "Выбери страну прокси:",
        reply_markup=countries_keyboard(is_admin(callback.from_user))
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

    flag, name = COUNTRIES[code]

    # Промежуточное сообщение пока идёт проверка
    await callback.message.edit_text(
        f"{flag} <b>{name}</b>\n\n⏳ Получаю прокси и проверяю...",
        parse_mode="HTML"
    )

    proxy_str       = build_proxy_string(code)
    alive, real_ip  = check_proxy(proxy_str)

    host, port, user, pwd = proxy_str.split(":", 3)
    url = f"http://{user}:{pwd}@{host}:{port}"

    if alive:
        status      = f"✅ Живой · IP: <code>{real_ip}</code>"
        answer_text = "Готово ✅"
    else:
        status      = "⚠️ Прокси не ответил — нажми 🔄 Другой IP"
        answer_text = "⚠️ Нет ответа"

    text = (
        f"{flag} <b>{name}</b>\n"
        f"{status}\n\n"
        f"<b>HOST:PORT:USER:PASS</b>\n<code>{proxy_str}</code>\n\n"
        f"<b>URL</b>\n<code>{url}</code>"
    )
    await callback.message.edit_text(text, reply_markup=proxy_result_kb(code), parse_mode="HTML")
    await callback.answer(answer_text)


# ── Добавить доступ ──────────────────────────────────────────────

@dp.callback_query(F.data == "add_access")
async def cb_add_access(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("⛔", show_alert=True)
        return
    await state.set_state(AdminFlow.waiting_add)
    await callback.message.edit_text(
        "Кому дать доступ?\nОтправь числовой ID или @username.",
        reply_markup=cancel_kb()
    )
    await callback.answer()


@dp.message(AdminFlow.waiting_add)
async def msg_add_access(message: Message, state: FSMContext):
    await state.clear()
    raw  = message.text.strip().lstrip("@")
    data = load_access()

    if raw.isdigit():
        uid = int(raw)
        if uid not in data["ids"]:
            data["ids"].append(uid)
            save_access(data)
            await message.answer(f"✅ Доступ выдан: <code>{uid}</code>",
                                  parse_mode="HTML", reply_markup=countries_keyboard(True))
        else:
            await message.answer("Этот ID уже есть.", reply_markup=countries_keyboard(True))
    else:
        uname = raw.lower()
        if uname not in [u.lower() for u in data["usernames"]]:
            data["usernames"].append(uname)
            save_access(data)
            await message.answer(f"✅ Доступ выдан: @{uname}", reply_markup=countries_keyboard(True))
        else:
            await message.answer("Этот username уже есть.", reply_markup=countries_keyboard(True))


# ── Убрать доступ ────────────────────────────────────────────────

@dp.callback_query(F.data == "remove_access")
async def cb_remove_access(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("⛔", show_alert=True)
        return
    await state.set_state(AdminFlow.waiting_remove)
    await callback.message.edit_text(
        "Кого убрать?\nОтправь числовой ID или @username.",
        reply_markup=cancel_kb()
    )
    await callback.answer()


@dp.message(AdminFlow.waiting_remove)
async def msg_remove_access(message: Message, state: FSMContext):
    await state.clear()
    raw  = message.text.strip().lstrip("@")
    data = load_access()

    if raw.isdigit():
        uid = int(raw)
        if uid in data["ids"]:
            data["ids"].remove(uid)
            save_access(data)
            await message.answer(f"✅ Убран: <code>{uid}</code>",
                                  parse_mode="HTML", reply_markup=countries_keyboard(True))
        else:
            await message.answer("Такого ID нет.", reply_markup=countries_keyboard(True))
    else:
        uname = raw.lower()
        if uname in [u.lower() for u in data["usernames"]]:
            data["usernames"] = [u for u in data["usernames"] if u.lower() != uname]
            save_access(data)
            await message.answer(f"✅ Убран: @{uname}", reply_markup=countries_keyboard(True))
        else:
            await message.answer("Такого username нет.", reply_markup=countries_keyboard(True))


# ── Список доступа ───────────────────────────────────────────────

@dp.callback_query(F.data == "list_access")
async def cb_list_access(callback: CallbackQuery):
    if not is_admin(callback.from_user):
        await callback.answer("⛔", show_alert=True)
        return
    data      = load_access()
    ids       = data.get("ids", [])
    usernames = data.get("usernames", [])

    lines = ["👥 <b>Список доступа</b>\n"]
    if ids:
        lines.append("<b>По ID:</b>")
        lines += [f"  • <code>{uid}</code>" for uid in ids]
    if usernames:
        lines.append("\n<b>По username:</b>")
        lines += [f"  • @{u}" for u in usernames]
    if not ids and not usernames:
        lines.append("Список пуст.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# ─────────────────────────────  ЗАПУСК  ─────────────────────────────

async def main():
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
