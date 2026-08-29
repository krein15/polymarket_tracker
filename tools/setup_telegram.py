#!/usr/bin/env python3
"""Настройка Telegram: проверка токена и автоопределение chat_id.

Избавляет от ручного хождения в браузер за getUpdates и вычитывания JSON.
Токен читается из .env и НИКОГДА не печатается — в вывод попадают только
имя бота и найденные chat_id.

Запуск из корня проекта (venv активировать не обязательно, нужен requests):

    python tools/setup_telegram.py             — проверить и записать chat_id
    python tools/setup_telegram.py --check     — только проверить, .env не трогать
    python tools/setup_telegram.py --test      — отправить тестовое сообщение

Порядок действий:
  1. Вписать TELEGRAM_BOT_TOKEN в .env (получить у @BotFather).
  2. Написать своему боту в Telegram любое сообщение — без этого он не имеет
     права тебе отвечать, и getUpdates вернёт пустой список.
  3. Запустить этот скрипт.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
TIMEOUT = 15


def read_env(path: Path) -> dict:
    """Простой парсер .env: KEY=VALUE, строки с # игнорируются."""
    if not path.exists():
        return {}
    data = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        data[key.strip()] = val.split("#")[0].strip()
    return data


def write_env_value(path: Path, key: str, value: str) -> None:
    """Заменить значение ключа в .env, сохранив комментарии и порядок строк."""
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def api(token: str, method: str, **params):
    """Вызов Bot API. Токен уходит только в URL запроса, в вывод не попадает."""
    r = requests.get(f"https://api.telegram.org/bot{token}/{method}",
                     params=params, timeout=TIMEOUT)
    try:
        data = r.json()
    except ValueError:
        print(f"[ОШИБКА] {method}: ответ не JSON, HTTP {r.status_code}")
        sys.exit(2)
    if not data.get("ok"):
        desc = data.get("description", "?")
        print(f"[ОШИБКА] {method}: {desc}")
        if "unauthorized" in desc.lower():
            print("Токен неверный. Возьми актуальный: @BotFather -> /mybots -> бот -> API Token")
        sys.exit(2)
    return data["result"]


def main() -> int:
    p = argparse.ArgumentParser(description="Проверка токена и автоопределение chat_id.")
    p.add_argument("--check", action="store_true", help="только проверить, .env не менять")
    p.add_argument("--test", action="store_true", help="отправить тестовое сообщение в чат")
    args = p.parse_args()

    env = read_env(ENV_PATH)
    token = env.get("TELEGRAM_BOT_TOKEN", "")

    if not ENV_PATH.exists():
        print(f"[ОШИБКА] Нет файла {ENV_PATH}")
        print("Создай его:  copy .env.example .env")
        return 1
    if not token:
        print("[ОШИБКА] TELEGRAM_BOT_TOKEN пуст в .env")
        print(f"Открой {ENV_PATH}, вставь токен от @BotFather и запусти скрипт снова.")
        return 1

    me = api(token, "getMe")
    print(f"Бот: @{me.get('username')} ({me.get('first_name')}), id={me.get('id')}")

    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if args.test:
        if not chat_id:
            print("[ОШИБКА] TELEGRAM_CHAT_ID пуст — сначала запусти без --test")
            return 1
        api(token, "sendMessage", chat_id=chat_id,
            text="Проверка связи: трекер видит бота и чат.")
        print(f"Тестовое сообщение отправлено в чат {chat_id} — проверь Telegram.")
        return 0

    updates = api(token, "getUpdates", limit=100)
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            title = chat.get("title") or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name")])
            ) or chat.get("username") or "?"
            chats[str(chat["id"])] = f"{title} ({chat.get('type')})"

    if not chats:
        print()
        print("getUpdates пуст — бот не видит ни одного чата.")
        print("Напиши своему боту в Telegram любое сообщение (например /start)")
        print("и запусти скрипт снова. Без этого Telegram не даёт боту писать тебе.")
        print()
        print("Если сообщения ты уже отправлял, но список пуст — значит апдейты")
        print("забрал другой процесс (запущенный трекер тоже их читает).")
        print("Останови трекер и попробуй ещё раз.")
        return 1

    print("\nНайденные чаты:")
    for cid, name in chats.items():
        print(f"  {cid}  — {name}")

    if len(chats) > 1:
        print("\nЧатов больше одного. Впиши нужный TELEGRAM_CHAT_ID в .env вручную.")
        return 1

    found = next(iter(chats))
    if args.check:
        print(f"\n--check: .env не тронут. Нужный TELEGRAM_CHAT_ID={found}")
        return 0
    if chat_id == found:
        print(f"\nTELEGRAM_CHAT_ID уже верный ({found}) — .env не меняю.")
        return 0

    write_env_value(ENV_PATH, "TELEGRAM_CHAT_ID", found)
    print(f"\nTELEGRAM_CHAT_ID={found} записан в .env")
    print("Проверить связь:  python tools/setup_telegram.py --test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
