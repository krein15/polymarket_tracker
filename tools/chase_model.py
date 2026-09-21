"""Можно ли предсказать погоню по признакам минуты 0.

Вопрос
------
Весь перевес проекта сидит в метке погони: покупки, за которыми рынок
пошёл, дают ROI +51.6% по цене трейдера, а все покупки подряд — +0.2%.
Но метка приходит на 29 минут позже сделки, и цена к тому времени уходит
на +59% медианы. Если метку удастся предсказать в минуту 0, входить можно
сразу по его цене: переплата падает с 59% до 2%.

Что делает
----------
Логистическая регрессия на признаках из `chase_features.py`. Без numpy:
14 742 строки на 12 признаках считаются за десятки секунд, а лишняя
зависимость ради этого не нужна.

Честность проверки
------------------
Разделение по ВРЕМЕНИ, а не случайное. Случайное перемешивание здесь
завышает качество: сделки одного матча идут подряд, похожи между собой и
попали бы и в обучение, и в проверку.

Главное число — не точность модели, а ROI отобранных сделок по цене
трейдера на проверочной половине. Модель, которая хорошо угадывает метку,
но не приносит денег, бесполезна.

Запуск:
    python tools\\chase_features.py --out ds.json
    python tools\\chase_model.py --load ds.json
"""
from __future__ import annotations

import argparse
import json
import math
import random

FEATURES = [
    ("impact", lambda d: d.get("impact") or 0.0),
    ("impact_big", lambda d: 1.0 if (d.get("impact") or 0) > 0.10 else 0.0),
    ("log_size", lambda d: math.log10(max(d["usdc"], 1.0))),
    ("size_vs_flow", lambda d: min(d.get("size_vs_flow") or 0.0, 1.0)),
    ("log_trades30", lambda d: math.log10(d["recent_trades"] + 1)),
    ("log_makers30", lambda d: math.log10(d["recent_makers"] + 1)),
    ("spread30", lambda d: d.get("spread30") or 0.0),
    ("first_in_token", lambda d: float(d["first_in_token"])),
    ("log_wallet_trades", lambda d: math.log10(d["wallet_trades"] + 1)),
    ("wallet_age_d", lambda d: min(d["wallet_age_h"] / 24.0, 7.0)),
    ("log_vol24", lambda d: math.log10(max(d.get("volume_24h") or 1.0, 1.0))),
    ("price", lambda d: d["price"]),
]


def matrix(rows):
    X = [[f(d) for _, f in FEATURES] for d in rows]
    y = [d["pos"] for d in rows]
    return X, y


def standardize(X, stats=None):
    """Привести признаки к одному масштабу. Без этого шаг градиента,
    подходящий для цены (0..1), не годится для объёма (10^6)."""
    k = len(X[0])
    if stats is None:
        mu = [sum(r[j] for r in X) / len(X) for j in range(k)]
        sd = []
        for j in range(k):
            v = sum((r[j] - mu[j]) ** 2 for r in X) / max(len(X) - 1, 1)
            sd.append(math.sqrt(v) or 1.0)
        stats = (mu, sd)
    mu, sd = stats
    return [[(r[j] - mu[j]) / sd[j] for j in range(k)] for r in X], stats


def fit(X, y, epochs=300, lr=0.3, l2=1e-3, seed=1):
    """Логистическая регрессия, полный градиент. Данных немного, и
    стохастика тут только добавила бы шума в сравнение моделей."""
    n, k = len(X), len(X[0])
    w = [0.0] * k
    b = 0.0
    for _ in range(epochs):
        gw = [0.0] * k
        gb = 0.0
        for i in range(n):
            z = b + sum(w[j] * X[i][j] for j in range(k))
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            e = p - y[i]
            gb += e
            xi = X[i]
            for j in range(k):
                gw[j] += e * xi[j]
        b -= lr * gb / n
        for j in range(k):
            w[j] -= lr * (gw[j] / n + l2 * w[j])
    return w, b


def predict(X, w, b):
    out = []
    for r in X:
        z = b + sum(w[j] * r[j] for j in range(len(w)))
        out.append(1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z)))))
    return out


def auc(y, p):
    """Доля правильно упорядоченных пар. 0.5 — монетка."""
    pos = [pi for pi, yi in zip(p, y) if yi]
    neg = [pi for pi, yi in zip(p, y) if not yi]
    if not pos or not neg:
        return float("nan")
    neg.sort()
    total = 0
    for x in pos:
        lo, hi = 0, len(neg)
        while lo < hi:                      # число neg строго меньше x
            mid = (lo + hi) // 2
            if neg[mid] < x:
                lo = mid + 1
            else:
                hi = mid
        eq = 0
        j = lo
        while j < len(neg) and neg[j] == x:
            eq += 1
            j += 1
        total += lo + eq * 0.5
    return total / (len(pos) * len(neg))


def money(rows):
    """ROI по цене трейдера среди закрывшихся. Это и есть ответ."""
    res = [d for d in rows if d["resolved"] and d["roi"] is not None]
    if len(res) < 20:
        return None, len(res), None
    v = [d["roi"] for d in res]
    m = sum(v) / len(v)
    se = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1) / len(v))
    return m, len(res), 1.96 * se


def show(label, rows):
    m, n, ci = money(rows)
    if m is None:
        print("    {:<30}{:>6} закрылось {} — мало".format(label, len(rows), n))
        return
    pos = sum(d["pos"] for d in rows) / len(rows) * 100
    neg = sum(d["neg"] for d in rows) / len(rows) * 100
    # Доля разворотов печатается рядом не для красоты: признаки движения
    # поднимают ОБЕ доли сразу, и по одной только погоне это незаметно.
    print("    {:<30}{:>6}  погонь {:>4.1f}%  разворотов {:>4.1f}%  "
          "ROI {:>+7.1f}% [{:>+6.1f};{:>+6.1f}]  (закрылось {})"
          .format(label, len(rows), pos, neg, m * 100,
                  (m - ci) * 100, (m + ci) * 100, n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", required=True)
    ap.add_argument("--split", type=float, default=0.7)
    ap.add_argument("--epochs", type=int, default=300)
    a = ap.parse_args()

    with open(a.load, encoding="utf-8") as f:
        rows = json.load(f)
    rows.sort(key=lambda d: d["ts"])
    cut = int(len(rows) * a.split)
    train, test = rows[:cut], rows[cut:]
    print("обучение {} сделок, проверка {} (разделение по времени)"
          .format(len(train), len(test)))
    print("доля погонь: обучение {:.1f}%, проверка {:.1f}%".format(
        sum(d["pos"] for d in train) / len(train) * 100,
        sum(d["pos"] for d in test) / len(test) * 100))

    Xtr, ytr = matrix(train)
    Xte, yte = matrix(test)
    Xtr, st = standardize(Xtr)
    Xte, _ = standardize(Xte, st)
    w, b = fit(Xtr, ytr, epochs=a.epochs)
    ptr = predict(Xtr, w, b)
    pte = predict(Xte, w, b)
    print()
    print("порядок различения (AUC): обучение {:.3f}, проверка {:.3f}"
          .format(auc(ytr, ptr), auc(yte, pte)))
    print("  0.50 — монетка; разрыв между обучением и проверкой = подгонка")

    print()
    print("вес признака (в единицах стандартного отклонения):")
    for (name, _), wj in sorted(zip(FEATURES, w), key=lambda t: -abs(t[1])):
        print("    {:<22}{:>+7.3f}".format(name, wj))

    order = sorted(range(len(test)), key=lambda i: -pte[i])
    print()
    print("ПРОВЕРОЧНАЯ половина: что даёт отбор по модели")
    print("    {:<34}{:>6}".format("группа", "n"))
    show("все сделки проверки", test)
    for frac in (0.05, 0.10, 0.20, 0.30):
        k = max(int(len(test) * frac), 1)
        show("верхние {:.0f}% по модели".format(frac * 100),
             [test[i] for i in order[:k]])
    show("нижние 30% по модели", [test[i] for i in order[-int(len(test) * 0.3):]])

    print()
    print("для сравнения — один признак вместо модели:")
    show("удар по цене > +10%", [d for d in test if (d.get("impact") or 0) > 0.10])
    show("сделка > 5% потока", [d for d in test if (d.get("size_vs_flow") or 0) > 0.05])

    print()
    print("случайный отбор того же размера (контроль):")
    rnd = random.Random(7)
    k = max(int(len(test) * 0.10), 1)
    show("случайные 10%", rnd.sample(test, k))


if __name__ == "__main__":
    main()
