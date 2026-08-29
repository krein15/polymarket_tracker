"""
debug_api.py — смотрим что реально возвращает /positions и /trades для одного адреса.

Запуск:
    python debug_api.py

Сохраняет сырые ответы API в debug_positions.json и debug_trades.json
чтобы понять структуру и починить analyze_whitelist.py
"""
import json
import requests

# Берём первый адрес из списка — LaBradfordSmith22
ADDRESS = "0x9495425feeb0c250accb89275c97587011b19a27"
DATA_API = "https://data-api.polymarket.com"

print(f"Проверяем адрес: {ADDRESS}\n")

# 1. /positions
print("=== GET /positions ===")
r = requests.get(f"{DATA_API}/positions", params={"user": ADDRESS, "limit": 10}, timeout=15)
print(f"Status: {r.status_code}")
positions = r.json()
print(f"Тип ответа: {type(positions).__name__}")
if isinstance(positions, list) and positions:
    print(f"Кол-во позиций: {len(positions)}")
    print("Первая позиция (все поля):")
    print(json.dumps(positions[0], indent=2, ensure_ascii=False))
    print("\nВсе уникальные ключи:")
    keys = set()
    for p in positions:
        keys.update(p.keys())
    print(sorted(keys))
elif isinstance(positions, dict):
    print("Ответ — словарь:")
    print(json.dumps(positions, indent=2, ensure_ascii=False)[:2000])
else:
    print("Пустой список или неожиданный формат")

with open("debug_positions.json", "w", encoding="utf-8") as f:
    json.dump(positions, f, ensure_ascii=False, indent=2)
print("\nСохранено в debug_positions.json")

# 2. /trades
print("\n=== GET /trades ===")
r2 = requests.get(f"{DATA_API}/trades", params={"maker": ADDRESS, "limit": 5}, timeout=15)
print(f"Status: {r2.status_code}")
trades = r2.json()
if isinstance(trades, list) and trades:
    print(f"Кол-во сделок: {len(trades)}")
    print("Первая сделка (все поля):")
    print(json.dumps(trades[0], indent=2, ensure_ascii=False))
    print("\nВсе уникальные ключи:")
    keys = set()
    for t in trades:
        keys.update(t.keys())
    print(sorted(keys))

with open("debug_trades.json", "w", encoding="utf-8") as f:
    json.dump(trades, f, ensure_ascii=False, indent=2)
print("\nСохранено в debug_trades.json")

print("\nГотово! Скинь оба файла в чат.")
