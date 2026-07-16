"""
Telegram-бот для выдачи прокси IPRoyal (residential) по странам.
Страны: Бельгия, Нидерланды, Греция.

Персистентность доступа: access.json хранится в GitHub-репозитории.
При добавлении/удалении людей бот коммитит изменения прямо в репо.
Нужны переменные окружения GITHUB_TOKEN и GITHUB_REPO.
"""

import asyncio
import base64
import json
import os
import secrets
from pathlib import Path

import requests as req_lib

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ─────────────────────────────  НАСТРОЙКИ  ─────────────────────────────

BOT_TOKEN      = os.environ["BOT_TOKEN"]
PROXY_HOST     = "geo.iproyal.com"
PROXY_PORT     = "32325"
PROXY_USERNAME = os.environ["PROXY_USERNAME"]
PROXY_PASSWORD = os.environ["PROXY_PASSWORD"]
SESSION_LIFETIME = "1h"

# GitHub — бот читает и пишет access.json прямо в репозиторий.
# Добавь GITHUB_TOKEN и GITHUB_REPO в переменные окружения на bothost.
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO        = os.environ.get("GITHUB_REPO", "")   # g36918250-spec/proxybot
GITHUB_BRANCH      = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_ACCESS_PATH = "access.json"

# Локальный fallback (если GitHub не настроен)
ACCESS_FILE = Path(os.environ.get("ACCESS_FILE_PATH", str(Path(__file__).with_name("access.json"))))

# Админы — зашиты в код, никогда не слетают.
ADMIN_USERNAMES: set[str] = {
    "bepowell", "trendbee", "patumkin", "ashlieq", "luparafuck",
}
ADMIN_IDS: set[int] = set()

# Сколько прокси выдавать — хранится в памяти, сбрасывается к 1 при перезапуске.
user_proxy_count: dict[int, int] = {}

def get_user_count(user_id: int) -> int:
    return user_proxy_count.get(user_id, 1)

def set_user_count(user_id: int, count: int) -> None:
    user_proxy_count[user_id] = count

# ─────────────────────────────  СТРАНЫ  ─────────────────────────────

COUNTRIES = {
    "nl": ("🇳🇱", "Нидерланды"),
    "be": ("🇧🇪", "Бельгия"),
    "at": ("🇦🇹", "Австрия"),
}

# ─────────────────────────────  GITHUB  ─────────────────────────────

def _gh_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def load_access() -> dict:
    """Загружает список доступа из GitHub (приоритет) или локального файла."""
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            r = req_lib.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ACCESS_PATH}",
                headers=_gh_headers(), timeout=10,
            )
            if r.status_code == 200:
                content = base64.b64decode(r.json()["content"]).decode("utf-8")
                return json.loads(content)
        except Exception as e:
            print(f"[WARN] GitHub load error: {e}")

    try:
        if ACCESS_FILE.exists():
            return json.loads(ACCESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"ids": [], "usernames": []}


def save_access(data: dict) -> None:
    """Сохраняет список доступа в GitHub и локально."""
    # Локальный файл (fallback)
    try:
        ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACCESS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[WARN] Local save error: {e}")

    # GitHub — основное хранилище
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_ACCESS_PATH}"
            r   = req_lib.get(url, headers=_gh_headers(), timeout=10)
            sha = r.json().get("sha") if r.status_code == 200 else None

            encoded = base64.b64encode(
                json.dumps(data, ensure_ascii=False, indent=2).encode()
            ).decode()
            payload: dict = {
                "message": "bot: обновление доступа",
                "content": encoded,
                "branch":  GITHUB_BRANCH,
            }
            if sha:
                payload["sha"] = sha

            result = req_lib.put(url, headers=_gh_headers(), json=payload, timeout=15)
            if result.status_code in (200, 201):
                print("[INFO] access.json → GitHub ✓")
            else:
                print(f"[WARN] GitHub save {result.status_code}: {result.text[:200]}")
        except Exception as e:
            print(f"[WARN] GitHub save error: {e}")

# ─────────────────────────────  ДОСТУП  ─────────────────────────────

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
    """Проверяет прокси через SOCKS5. Таймаут 2 сек."""
    host, port, user, pwd = proxy_str.split(":", 3)
    proxy_url = f"socks5://{user}:{pwd}@{host}:{port}"
    proxies   = {"http": proxy_url, "https": proxy_url}
    try:
        r  = req_lib.get("https://api.ipify.org?format=json", proxies=proxies, timeout=2)
        ip = r.json().get("ip", "?")
        return True, ip
    except Exception:
        return False, ""


async def check_proxy_async(proxy_str: str) -> tuple[bool, str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, check_proxy, proxy_str)


async def collect_working_proxies(code: str, needed: int) -> list[tuple[str, str]]:
    """Собирает нужное кол-во РАБОЧИХ прокси, нерабочие пропускает.
    Проверяет пачками, максимум 5 раундов."""
    working: list[tuple[str, str]] = []
    for _ in range(5):
        if len(working) >= needed:
            break
        still_need = needed - len(working)
        batch      = [build_proxy_string(code) for _ in range(still_need)]
        results    = await asyncio.gather(*[check_proxy_async(p) for p in batch])
        for proxy_str, (alive, ip) in zip(batch, results):
            if alive:
                working.append((proxy_str, ip))
    return working

# ─────────────────────────────  КЛАВИАТУРЫ  ─────────────────────────────

def countries_keyboard(admin: bool = False, user_id: int = 0) -> InlineKeyboardMarkup:
    count = get_user_count(user_id)
    rows = [
        [InlineKeyboardButton(text=f"{flag} {name}", callback_data=f"country:{code}")]
        for code, (flag, name) in COUNTRIES.items()
    ]
    rows.append([InlineKeyboardButton(text=f"🔢 Количество прокси: {count}", callback_data="count_menu")])
    if admin:
        rows += [
            [InlineKeyboardButton(text="➕ Добавить доступ", callback_data="add_access")],
            [InlineKeyboardButton(text="➖ Убрать доступ",   callback_data="remove_access")],
            [InlineKeyboardButton(text="👥 Список доступа",  callback_data="list_access")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def count_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(i), callback_data=f"set_count:{i}") for i in range(1, 6)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="back")]
    ])


def proxy_result_kb(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Другой IP",        callback_data=f"country:{code}")],
        [InlineKeyboardButton(text="⬅️ К списку стран",  callback_data="back")],
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
    uid = message.from_user.id
    await message.answer(
        "Выбери страну прокси:",
        reply_markup=countries_keyboard(is_admin(message.from_user), uid)
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    await message.answer("Отменено.", reply_markup=countries_keyboard(is_admin(message.from_user), uid))


@dp.callback_query(F.data == "back")
async def cb_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = callback.from_user.id
    await callback.message.edit_text(
        "Выбери страну прокси:",
        reply_markup=countries_keyboard(is_admin(callback.from_user), uid)
    )
    await callback.answer()


@dp.callback_query(F.data == "count_menu")
async def cb_count_menu(callback: CallbackQuery):
    if not is_allowed(callback.from_user):
        await callback.answer("⛔", show_alert=True)
        return
    await callback.message.edit_text(
        "Выбери количество прокси за один запрос:",
        reply_markup=count_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("set_count:"))
async def cb_set_count(callback: CallbackQuery):
    if not is_allowed(callback.from_user):
        await callback.answer("⛔", show_alert=True)
        return
    count = int(callback.data.split(":")[1])
    uid   = callback.from_user.id
    set_user_count(uid, count)
    await callback.message.edit_text(
        "Выбери страну прокси:",
        reply_markup=countries_keyboard(is_admin(callback.from_user), uid)
    )
    await callback.answer(f"✅ Теперь выдаётся {count} шт.")


@dp.callback_query(F.data.startswith("country:"))
async def cb_country(callback: CallbackQuery):
    if not is_allowed(callback.from_user):
        await callback.answer("⛔ Доступ ограничен.", show_alert=True)
        return

    code = callback.data.split(":", 1)[1]
    if code not in COUNTRIES:
        await callback.answer("Неизвестная страна.", show_alert=True)
        return

    uid        = callback.from_user.id
    count      = get_user_count(uid)
    flag, name = COUNTRIES[code]
    label      = f"{flag} <b>{name}</b>" + (f" · {count} шт." if count > 1 else "")

    await callback.message.edit_text(
        f"{label}\n\n⏳ Ищу рабочие прокси...",
        parse_mode="HTML"
    )

    working = await collect_working_proxies(code, count)

    if not working:
        await callback.message.edit_text(
            f"{label}\n\n⚠️ Не удалось найти рабочие прокси. Попробуй ещё раз.",
            reply_markup=proxy_result_kb(code),
            parse_mode="HTML"
        )
        await callback.answer("⚠️ Нет рабочих прокси")
        return

    blocks = []
    for i, (proxy_str, ip) in enumerate(working, 1):
        num = f"<b>{i}.</b> " if count > 1 else ""
        host, port, user, pwd = proxy_str.split(":", 3)
        url = f"socks5://{user}:{pwd}@{host}:{port}"
        blocks.append(
            f"{num}✅ <code>{ip}</code>\n"
            f"<code>{proxy_str}</code>\n"
            f"<code>{url}</code>"
        )

    found = len(working)
    header = label + (f" · найдено {found}/{count}" if found < count else "")
    text = header + "\n\n" + "\n\n".join(blocks)

    await callback.message.edit_text(text, reply_markup=proxy_result_kb(code), parse_mode="HTML")
    await callback.answer(f"✅ {found} шт.")


# ── Добавить доступ ──────────────────────────────────────────────

@dp.callback_query(F.data == "add_access")
async def cb_add_access(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("⛔", show_alert=True)
        return
    await state.set_state(AdminFlow.waiting_add)
    await callback.message.edit_text(
        "Кому дать доступ?\n"
        "Можно сразу несколько через запятую:\n"
        "<code>@username1, 123456789, username2</code>",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )
    await callback.answer()


@dp.message(AdminFlow.waiting_add)
async def msg_add_access(message: Message, state: FSMContext):
    await state.clear()
    entries          = [e.strip().lstrip("@") for e in message.text.split(",")]
    data             = load_access()
    added, already   = [], []

    for raw in entries:
        if not raw:
            continue
        if raw.isdigit():
            uid = int(raw)
            if uid not in data["ids"]:
                data["ids"].append(uid)
                added.append(f"<code>{uid}</code>")
            else:
                already.append(f"<code>{uid}</code>")
        else:
            uname = raw.lower()
            if uname not in [u.lower() for u in data["usernames"]]:
                data["usernames"].append(uname)
                added.append(f"@{uname}")
            else:
                already.append(f"@{uname}")

    if added:
        save_access(data)

    lines = []
    if added:
        lines.append(f"✅ Добавлены: {', '.join(added)}")
    if already:
        lines.append(f"Уже были: {', '.join(already)}")
    await message.answer(
        "\n".join(lines) or "Никого не добавлено.",
        parse_mode="HTML",
        reply_markup=countries_keyboard(True, message.from_user.id)
    )


# ── Убрать доступ ────────────────────────────────────────────────

@dp.callback_query(F.data == "remove_access")
async def cb_remove_access(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("⛔", show_alert=True)
        return
    await state.set_state(AdminFlow.waiting_remove)
    await callback.message.edit_text(
        "Кого убрать?\n"
        "Можно несколько через запятую:\n"
        "<code>@username1, 123456789</code>",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )
    await callback.answer()


@dp.message(AdminFlow.waiting_remove)
async def msg_remove_access(message: Message, state: FSMContext):
    await state.clear()
    entries              = [e.strip().lstrip("@") for e in message.text.split(",")]
    data                 = load_access()
    removed, not_found   = [], []

    for raw in entries:
        if not raw:
            continue
        if raw.isdigit():
            uid = int(raw)
            if uid in data["ids"]:
                data["ids"].remove(uid)
                removed.append(f"<code>{uid}</code>")
            else:
                not_found.append(f"<code>{uid}</code>")
        else:
            uname = raw.lower()
            if uname in [u.lower() for u in data["usernames"]]:
                data["usernames"] = [u for u in data["usernames"] if u.lower() != uname]
                removed.append(f"@{uname}")
            else:
                not_found.append(f"@{uname}")

    if removed:
        save_access(data)

    lines = []
    if removed:
        lines.append(f"✅ Убраны: {', '.join(removed)}")
    if not_found:
        lines.append(f"Не найдены: {', '.join(not_found)}")
    await message.answer(
        "\n".join(lines) or "Никого не убрано.",
        parse_mode="HTML",
        reply_markup=countries_keyboard(True, message.from_user.id)
    )


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
