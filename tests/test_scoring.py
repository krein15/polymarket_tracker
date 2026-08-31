"""Скоринг: свёртка признаков в балл (чистая функция, без БД)."""
from __future__ import annotations

from conftest import NOW  # noqa: F401  (нужен для единообразия импортов)

from polymarket_tracker.scoring import Features, compute_score


def features(**kw) -> Features:
    base = dict(
        accumulated_usdc=1000.0,
        accumulation_trades=1,
        baseline_hourly=1000.0,
        market_relative=None,
        dormant_days=None,
        cluster_wallets=0,
        cluster_all_wallets=0,
        history_days=30.0,  # истории достаточно: признаки кошелька включены
        is_new_wallet=False,
        is_market_maker=False,
        price=0.5,
        volume_24h=0.0,
    )
    base.update(kw)
    return Features(**base)


class TestMarketRelative:
    def test_ниже_обычного_оборота_баллов_нет(self, config):
        s = compute_score(features(market_relative=0.5), config)
        assert "market_relative" not in s.parts

    def test_баллы_растут_ступенями(self, config):
        got = [
            compute_score(features(market_relative=r), config).parts.get("market_relative", 0)
            for r in (1.0, 3.0, 10.0, 30.0, 100.0)
        ]
        assert got == [10, 18, 26, 35, 35]

    def test_без_базового_оборота_признак_молчит(self, config):
        """Если сравнивать не с чем — не выдумываем баллы."""
        s = compute_score(features(market_relative=None), config)
        assert "market_relative" not in s.parts


class TestAccumulation:
    def test_одна_покупка_не_считается_набором(self, config):
        s = compute_score(features(accumulation_trades=1), config)
        assert "accumulation" not in s.parts

    def test_дробление_ордера_даёт_баллы(self, config):
        assert compute_score(features(accumulation_trades=3), config).parts["accumulation"] == 10
        assert compute_score(features(accumulation_trades=6), config).parts["accumulation"] == 15


class TestDormancy:
    def test_короткая_пауза_не_считается(self, config):
        s = compute_score(features(dormant_days=5.0), config)
        assert "dormant_wake" not in s.parts

    def test_месяц_и_квартал_молчания(self, config):
        assert compute_score(features(dormant_days=30.0), config).parts["dormant_wake"] == 10
        assert compute_score(features(dormant_days=120.0), config).parts["dormant_wake"] == 15


class TestClusterAndPrice:
    def test_кластер_ступенями(self, config):
        got = [compute_score(features(cluster_wallets=n), config).parts.get("cluster", 0)
               for n in (2, 3, 5, 8)]
        assert got == [0, 10, 15, 20]

    def test_дешёвый_хвост(self, config):
        assert compute_score(features(price=0.10), config).parts["cheap_tail"] == 10
        assert compute_score(features(price=0.22), config).parts["cheap_tail"] == 5
        assert "cheap_tail" not in compute_score(features(price=0.60), config).parts


class TestPenalties:
    def test_маркет_мейкер_фактически_дисквалифицирует(self, config):
        """Сильный набор признаков не должен пробить порог, если кошелёк
        торгует обе стороны рынка."""
        f = features(market_relative=30.0, is_new_wallet=True, cluster_wallets=8,
                     is_market_maker=True)
        s = compute_score(f, config)
        assert s.parts["market_maker"] == -40
        assert s.total < config.score_threshold

    def test_почти_решённый_рынок_штрафуется(self, config):
        s = compute_score(features(price=0.97), config)
        assert s.parts["near_resolved"] == -25
        assert "cheap_tail" not in s.parts

    def test_без_штрафов_сильный_набор_проходит_порог(self, config):
        f = features(market_relative=10.0, accumulation_trades=6, is_new_wallet=True,
                     cluster_wallets=5, price=0.12, volume_24h=1000.0)
        assert compute_score(f, config).total >= config.score_threshold


class TestColdStart:
    """На свежей БД «новый кошелёк» значит лишь «мы его не видели»: замер на
    живых данных — история 1.4 часа, 95% кошельков «новые». Признаки кошелька
    в этом режиме давали 35 баллов даром и топили порог."""

    def test_молодая_история_не_даёт_баллов_за_новизну(self, config):
        f = features(history_days=0.1, is_new_wallet=True, cluster_wallets=8)
        s = compute_score(f, config)
        assert "wallet_new" not in s.parts
        assert any("истории" in n for n in s.notes)

    def test_кластер_считается_и_на_молодой_бд(self, config):
        """Кластер больше не про новизну кошельков, а про деньги: сколько
        РАЗНЫХ адресов зашли крупно. Это видно в своей базе сразу и от
        накопленной истории не зависит."""
        f = features(history_days=0.1, cluster_wallets=8)
        assert compute_score(f, config).parts.get("cluster") == 20

    def test_признаки_потока_работают_и_на_молодой_бд(self, config):
        f = features(history_days=0.1, market_relative=10.0, price=0.1,
                     volume_24h=1000.0, accumulation_trades=6)
        s = compute_score(f, config)
        assert set(s.parts) == {"market_relative", "cheap_tail", "illiquid_market", "accumulation"}

    def test_накопив_историю_признаки_кошелька_включаются(self, config):
        f = lambda days: features(history_days=days, is_new_wallet=True, cluster_wallets=5)
        # До порога истории засчитывается только кластер (он от неё не зависит),
        # после — добавляется новизна кошелька.
        assert compute_score(f(2.9), config).total == 15
        assert compute_score(f(3.0), config).total == 30


class TestPresentation:
    def test_разбивка_сортируется_по_вкладу_и_сериализуется(self, config):
        f = features(market_relative=30.0, is_new_wallet=True, cluster_wallets=3)
        s = compute_score(f, config)
        assert s.summary().startswith("market_relative +35")
        import json
        assert json.loads(s.parts_json())["market_relative"] == 35

    def test_пустой_балл_не_ломает_вывод(self, config):
        s = compute_score(features(), config)
        assert s.total == 0.0
        assert s.summary() == ""
