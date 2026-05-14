"""
debug_api2.py — ищем правильный эндпоинт для winrate трейдера.

Запуск:
    python debug_api2.py

Проверяем несколько эндпоинтов и сохраняем ответы.
"""
import json
import requests

# Берём трейдера с известным хорошим P&L — orangexyz
ADDRESS = "0x160771e1041ea85cb780f6f9216de8e56259121e"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

TIMEOUT = 15

def try_endpoint(url, params=None, label=""):
    print(f"\n=== {label} ===")
    print(f"URL: {url}")
    print(f"Params: {params}")
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        print(f"Status: {r.status_code}")
        data = r.json()
        if isinstance(data, list):
            print(f"Список из {len(data)} элементов")
            if data:
                print("Первый элемент:")
                print(json.dumps(data[0], indent=2, ensure_ascii=False)[:1000])
                print("Ключи:", sorted(data[0].keys()) if isinstance(data[0], dict) else "не dict")
        elif isinstance(data, dict):
            print("Словарь, ключи:", sorted(data.keys()))
            print(json.dumps(data, indent=2, ensure_ascii=False)[:1500])
        else:
            print(f"Тип: {type(data)}, значение: {str(data)[:500]}")
        return data
    except Exception as e:
        print(f"Ошибка: {e}")
        return None

results = {}

# 1. Профиль пользователя
results["profile"] = try_endpoint(
    f"{DATA_API}/profile",
    {"user": ADDRESS},
    "Profile"
)

# 2. Value / PnL
results["value"] = try_endpoint(
    f"{DATA_API}/value",
    {"user": ADDRESS},
    "Value/PnL"
)

# 3. Activity
results["activity"] = try_endpoint(
    f"{DATA_API}/activity",
    {"user": ADDRESS, "limit": 5},
    "Activity"
)

# 4. Portfolio
results["portfolio"] = try_endpoint(
    f"{DATA_API}/portfolio",
    {"user": ADDRESS},
    "Portfolio"
)

# 5. Markets traded
results["markets"] = try_endpoint(
    f"{DATA_API}/markets",
    {"user": ADDRESS, "limit": 5},
    "Markets"
)

# 6. Gamma profile
results["gamma_profile"] = try_endpoint(
    f"{GAMMA_API}/profile",
    {"address": ADDRESS},
    "Gamma Profile"
)

# 7. Gamma user stats
results["gamma_user"] = try_endpoint(
    f"{GAMMA_API}/users",
    {"address": ADDRESS},
    "Gamma Users"
)

# Сохраняем всё
with open("debug_api2.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print("\n\nВсё сохранено в debug_api2.json — скинь мне!")
