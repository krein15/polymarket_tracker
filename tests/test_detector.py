"""Детектор: ворота Ветки S, порог балла, whitelist и параллельная Ветка A."""
from __future__ import annotations

from conftest import NOW, make_market, make_trade, make_wallet

from polymarket_tracker.anomaly_detector import AnomalyDetector


class FakeWatchlist:
    def __init__(self, addresses=()):
        self._a = {a.lower() for a in addresses}

    def is_whitelisted(self, address: str) -> bool:
        return address.lower() in self._a

    def __len__(self):
        return len(self._a)


def detector(config, storage, whitelist=()):
    return AnomalyDetector(config, storage, FakeWatchlist(whitelist))


def save(storage, trade):
    storage.save_trade(
        tx_hash=trade.tx_hash, log_index=0, ts=trade.timestamp, block_number=0,
        maker=trade.maker, token_id=trade.token_id, side=trade.side,
        usdc_amount=trade.usdc_amount, price=trade.price,
    )


class TestHardGates:
    def test_продажа_не_оценивается(self, config, storage):
        t = make_trade(side="sell", usdc=9000)
        save(storage, t)
        r = detector(config, storage).evaluate(t, make_market(), make_wallet())
        assert r.score is None and r.signals == []

    def test_мелочь_ниже_предфильтра_не_оценивается(self, config, storage):
        t = make_trade(usdc=100.0)
        save(storage, t)
        r = detector(config, storage).evaluate(t, make_market(), make_wallet())
        assert r.score is None

    def test_закрытый_рынок_не_оценивается(self, config, storage):
        t = make_trade()
        save(storage, t)
        r = detector(config, storage).evaluate(t, make_market(closed=True), make_wallet())
        assert r.score is None

    def test_без_метаданных_рынка_только_whitelist(self, config, storage):
        t = make_trade()
        save(storage, t)
        r = detector(config, storage).evaluate(t, None, make_wallet())
        assert r.score is None and r.signals == []


class TestCategoryFilter:
    def test_спорт_режется(self, config, storage):
        t = make_trade()
        save(storage, t)
        market = make_market(category="sports", tags=("nba", "basketball", "sports"))
        assert detector(config, storage).evaluate(t, market, make_wallet()).score is None

    def test_киберспорт_проходит_при_явном_разрешении(self, config, storage):
        """Рынки киберспорта помечены и как sports — вернуть их можно только
        списком исключений, иначе откроется весь обычный спорт."""
        config.allowed_tags = {"esports"}
        t = make_trade()
        save(storage, t)
        market = make_market(category="sports", tags=("esports", "league-of-legends", "sports"))
        assert detector(config, storage).evaluate(t, market, make_wallet()).score is not None

    def test_обычный_спорт_остаётся_отрезанным_при_разрешённом_киберспорте(self, config, storage):
        config.allowed_tags = {"esports"}
        t = make_trade()
        save(storage, t)
        market = make_market(category="sports", tags=("nba", "sports"))
        assert detector(config, storage).evaluate(t, market, make_wallet()).score is None


class TestThreshold:
    def test_балл_ниже_порога_сигнала_не_даёт_но_считается(self, config, storage):
        """Балл нужен даже без сигнала: на нём калибруется порог."""
        config.score_threshold = 200.0  # заведомо недостижимо
        t = make_trade()
        save(storage, t)
        r = detector(config, storage).evaluate(t, make_market(), make_wallet())
        assert r.score is not None
        assert r.signals == []

    def test_балл_выше_порога_даёт_сигнал_score(self, config, storage):
        config.score_threshold = 1.0
        t = make_trade()
        save(storage, t)
        r = detector(config, storage).evaluate(t, make_market(), make_wallet())
        assert [s.signal_type for s in r.signals] == ["score"]
        assert r.signals[0].score is r.score
        assert "Балл" in r.signals[0].reason

    def test_выключенный_скоринг_не_считает(self, config, storage):
        config.scoring_enabled = False
        t = make_trade()
        save(storage, t)
        assert detector(config, storage).evaluate(t, make_market(), make_wallet()).score is None


class TestWhitelistBranch:
    def test_whitelist_сигналит_независимо_от_балла(self, config, storage):
        config.score_threshold = 200.0
        maker = "0x" + "7" * 40
        t = make_trade(maker=maker, usdc=500.0)
        save(storage, t)
        r = detector(config, storage, whitelist=[maker]).evaluate(t, make_market(), make_wallet())
        assert [s.signal_type for s in r.signals] == ["whitelist"]

    def test_мелкая_сделка_из_whitelist_не_сигналит(self, config, storage):
        maker = "0x" + "7" * 40
        t = make_trade(maker=maker, usdc=10.0)
        save(storage, t)
        r = detector(config, storage, whitelist=[maker]).evaluate(t, make_market(), make_wallet())
        assert r.signals == []


class TestLegacyBranchInParallel:
    def test_прежняя_ветка_a_считается_но_не_шлёт(self, config, storage):
        """Вердикт старой цепочки И нужен теневой выборке для сравнения
        методик, но сигналов она больше не порождает."""
        config.score_threshold = 200.0
        t = make_trade(usdc=5000.0, price=0.5)
        save(storage, t)
        market = make_market(volume_24h=10_000.0)
        r = detector(config, storage).evaluate(t, market, make_wallet(is_new=True))
        assert r.legacy_passed is True
        assert r.legacy_types == ["suspicious_entry"]
        assert r.signals == []  # сигналов от Ветки A нет

    def test_ликвидный_рынок_прежнюю_ветку_не_проходит(self, config, storage):
        t = make_trade(usdc=5000.0)
        save(storage, t)
        market = make_market(volume_24h=500_000.0)
        r = detector(config, storage).evaluate(t, market, make_wallet(is_new=True))
        assert r.legacy_passed is False

    def test_старый_кошелёк_без_кластера_прежнюю_ветку_не_проходит(self, config, storage):
        t = make_trade(usdc=5000.0)
        save(storage, t)
        r = detector(config, storage).evaluate(t, make_market(), make_wallet(is_new=False))
        assert r.legacy_passed is False


class TestAccumulationEndToEnd:
    def test_набор_позиции_частями_виден_детектору(self, config, storage):
        """Главный пропуск прежней методики: двадцать покупок по $300 не
        проходили порог $2000 на одну сделку и были невидимы."""
        maker = "0x" + "b" * 40
        for i in range(6):
            t = make_trade(maker=maker, usdc=300.0, ts=NOW - (5 - i) * 60, tx_hash=f"0x{i}")
            save(storage, t)
        last = make_trade(maker=maker, usdc=300.0, ts=NOW, tx_hash="0xlast")
        save(storage, last)

        r = detector(config, storage).evaluate(last, make_market(), make_wallet())
        assert r.score is not None
        assert r.score.parts.get("accumulation") == 15
        # прежняя цепочка И такую сделку не увидела бы вовсе
        assert r.legacy_passed is False
