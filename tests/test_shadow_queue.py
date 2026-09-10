"""Очередь теневых сделок: просроченные снимки не должны голодать.

Чем это было
------------
Очередь была чистым LRU — "дольше всех не проверявшиеся первыми". У свежей
сделки last_checked_ts почти сейчас, поэтому она уходила в хвост, а впереди
стояли тысячи старых. Час истекал раньше, чем до неё доходил черёд, и если
рынок за это время закрывался, снимок цены через час не брался никогда.

Видно по живым данным (21 340 теневых сделок):

    рынок закрылся < 1 часа     726 сделок,  снимок есть у   0.0%
    3-12 часов                 9992                         12.7%
    больше 2 суток              507                         98.4%

Для рынков быстрее часа снимка и быть не может. Но 3-12 часов живут заметно
дольше часа — там 12.7% это потеря, причём смещённая: замеры дрейфа считались
почти только по медленным рынкам, а поток трекера — быстрый спорт.
"""
from __future__ import annotations

from conftest import NOW

HOUR = 3600


def shadow(storage, tx, ts, checked=None, price_1h=None, resolved=0):
    storage.save_shadow_trade(
        tx_hash=tx, maker="0x" + "a" * 40, token_id=f"tok-{tx}", ts=ts,
        side="buy", usdc_amount=5000.0, price=0.40, market_slug=f"m-{tx}",
        category="politics", volume_24h=10000.0, passed_filters=False,
        signal_types=None, now_ts=ts, score=50.0, score_parts=None,
    )
    sets, args = [], []
    if checked is not None:
        sets.append("last_checked_ts = ?"); args.append(checked)
    if price_1h is not None:
        sets.append("price_1h = ?"); args.append(price_1h)
    if resolved:
        sets.append("market_resolved = 1")
    if sets:
        with storage._conn() as c:
            c.execute(f"UPDATE shadow_trades SET {', '.join(sets)} WHERE tx_hash = ?",
                      (*args, tx))


class TestOverduePriority:
    def test_просроченный_снимок_обгоняет_старую_очередь(self, storage):
        """Ровно тот случай, который терял данные: один свежий кандидат
        против длинного хвоста давно не проверявшихся."""
        for i in range(40):
            shadow(storage, f"old{i}", NOW - 10 * 86400, checked=NOW - 5 * 86400)
        shadow(storage, "fresh", NOW - 90 * 60, checked=NOW - 90 * 60)

        got = storage.get_shadow_to_update(limit=10, now_ts=NOW)
        assert "fresh" in {r["token_id"].removeprefix("tok-") for r in got}

    def test_безнадёжно_старые_не_топят_свежих(self, storage):
        """Хвост, у которого час истёк давно, в срочную половину не идёт:
        иначе он вытеснил бы тех, чей снимок ещё что-то значит."""
        for i in range(40):
            shadow(storage, f"stale{i}", NOW - 5 * 86400, checked=NOW - 5 * 86400)
        shadow(storage, "fresh", NOW - 90 * 60, checked=NOW - 90 * 60)

        got = storage.get_shadow_to_update(limit=4, now_ts=NOW)
        assert "fresh" in {r["token_id"].removeprefix("tok-") for r in got}

    def test_ещё_не_просрочен_ждёт_как_все(self, storage):
        """Час не прошёл — снимать нечего, лезть без очереди незачем."""
        for i in range(20):
            shadow(storage, f"old{i}", NOW - 10 * 86400, checked=NOW - 5 * 86400)
        shadow(storage, "young", NOW - 600, checked=NOW - 600)

        got = storage.get_shadow_to_update(limit=5, now_ts=NOW)
        assert "young" not in {r["token_id"].removeprefix("tok-") for r in got}

    def test_уже_снятые_идут_после_срочных(self, storage):
        """Снимок у неё есть, спешить некуда — но из очереди на проверку
        резолва она не выпадает, поэтому важен именно порядок."""
        shadow(storage, "done", NOW - 100 * 60, checked=NOW - 100 * 60, price_1h=0.5)
        shadow(storage, "todo", NOW - 90 * 60, checked=NOW - 90 * 60)
        got = storage.get_shadow_to_update(limit=2, now_ts=NOW)
        names = [r["token_id"].removeprefix("tok-") for r in got]
        assert names[0] == "todo"

    def test_закрытые_рынки_не_возвращаются(self, storage):
        shadow(storage, "closed", NOW - 90 * 60, checked=NOW - 4 * HOUR, resolved=1)
        assert storage.get_shadow_to_update(limit=5, now_ts=NOW) == []


class TestQueueSplit:
    def test_половина_остаётся_обычной_очереди(self, storage):
        """Отдать всю квоту просрочке значило бы остановить проверку резолва,
        пока разбирается накопленный хвост."""
        for i in range(30):
            shadow(storage, f"due{i}", NOW - 90 * 60 + i, checked=NOW - 3 * HOUR)
        for i in range(30):
            shadow(storage, f"lru{i}", NOW - 20 * 86400, checked=NOW - 9 * 86400 - i)

        got = storage.get_shadow_to_update(limit=10, now_ts=NOW)
        names = [r["token_id"].removeprefix("tok-") for r in got]
        assert sum(1 for n in names if n.startswith("lru")) >= 5

    def test_строки_не_дублируются(self, storage):
        """Одна и та же сделка проходит по обоим условиям; вернуть её дважды
        значило бы потратить половину батча впустую."""
        for i in range(6):
            shadow(storage, f"both{i}", NOW - 90 * 60, checked=NOW - 3 * HOUR)
        got = storage.get_shadow_to_update(limit=10, now_ts=NOW)
        ids = [r["shadow_id"] for r in got]
        assert len(ids) == len(set(ids))

    def test_предел_батча_соблюдается(self, storage):
        for i in range(30):
            shadow(storage, f"due{i}", NOW - 90 * 60, checked=NOW - 3 * HOUR)
        assert len(storage.get_shadow_to_update(limit=8, now_ts=NOW)) == 8

    def test_поля_совпадают_у_обеих_половин(self, storage):
        """Общий обработчик читает их по одним и тем же ключам."""
        shadow(storage, "due", NOW - 90 * 60, checked=NOW - 3 * HOUR)
        shadow(storage, "lru", NOW - 40 * 86400, checked=NOW - 9 * 86400, price_1h=0.5)
        got = storage.get_shadow_to_update(limit=4, now_ts=NOW)
        assert len(got) == 2
        assert set(got[0]) == set(got[1])
        for r in got:
            assert {"shadow_id", "token_id", "side", "price_at_signal",
                    "signal_ts", "has_price_1h"} <= set(r)

    def test_момент_отсчёта_по_умолчанию_текущий(self, storage):
        """Парный метод для боевых сигналов такого параметра не принимает,
        а общий обработчик зовёт оба одинаково."""
        import time
        shadow(storage, "due", int(time.time()) - 90 * 60)
        assert len(storage.get_shadow_to_update(limit=4)) == 1
