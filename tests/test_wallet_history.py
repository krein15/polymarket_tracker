"""История кошелька из API и адаптивный порог.

Смысл: признаки новизны, пробуждения и кластера — треть скоринга — раньше
молчали первые трое суток из-за правила холодного старта. Замер за сутки
работы: ни одного срабатывания, достижимый максимум 70 при пороге 58.
"""
from __future__ import annotations

import time

from conftest import NOW

from polymarket_tracker.scoring import Features, compute_score
from polymarket_tracker.wallet_history import WalletHistory

ADDR = "0x" + "a" * 40


def history(first_ts, count, complete, recent=()):
    return WalletHistory(
        address=ADDR, first_trade_ts=first_ts, trades_on_page=count,
        complete=complete, recent_ts=tuple(recent), fetched_at=time.time(),
    )


class TestWalletHistory:
    def test_молодой_и_редкий_кошелёк_считается_новым(self):
        h = history(NOW - 5 * 86400, 7, complete=True)
        assert h.is_new(max_trades=20, max_age_days=30, now=NOW) is True

    def test_старый_кошелёк_не_новый(self):
        h = history(NOW - 200 * 86400, 5, complete=True)
        assert h.is_new(20, 30, NOW) is False

    def test_много_сделок_не_новый(self):
        h = history(NOW - 5 * 86400, 300, complete=True)
        assert h.is_new(20, 30, NOW) is False

    def test_неполная_история_сразу_не_новый(self):
        """Не уместилось 500 событий — порог по числу сделок заведомо взят,
        точная цифра для отказа не нужна."""
        h = history(NOW - 5 * 86400, 500, complete=False)
        assert h.is_new(20, 30, NOW) is False

    def test_возраст_в_днях(self):
        h = history(NOW - 10 * 86400, 3, complete=True)
        assert 9.9 < h.age_days(NOW) < 10.1

    def test_предыдущая_сделка_для_пробуждения(self):
        h = history(NOW - 100 * 86400, 3, complete=True,
                    recent=(NOW, NOW - 90 * 86400, NOW - 95 * 86400))
        assert h.prev_trade_ts(NOW) == NOW - 90 * 86400

    def test_предыдущей_нет(self):
        h = history(NOW, 1, complete=True, recent=(NOW,))
        assert h.prev_trade_ts(NOW) is None


def features(**kw):
    base = dict(
        accumulated_usdc=5000.0, accumulation_trades=1, baseline_hourly=100.0,
        market_relative=5.0, dormant_days=None, cluster_wallets=0,
        cluster_all_wallets=1, history_days=0.1, is_new_wallet=False,
        is_market_maker=False, price=0.5, volume_24h=1000.0,
    )
    base.update(kw)
    return Features(**base)


class TestAvailableMax:
    """Достижимый максимум зависит от того, что вообще можно посчитать."""

    def test_холодный_старт_без_признаков_кошелька(self, config):
        s = compute_score(features(), config)
        assert s.available_max == 90

    def test_история_из_api_открывает_признаки_кошелька(self, config):
        s = compute_score(features(wallet_from_api=True), config)
        assert s.available_max == 120

    def test_локальная_история_тоже_открывает(self, config):
        s = compute_score(features(history_days=5.0), config)
        assert s.available_max == 120

    def test_whitelist_добавляет_свой_потолок(self, config):
        s = compute_score(features(wallet_from_api=True, whitelist_tier="pass"), config)
        assert s.available_max == 150

    def test_порог_ниже_когда_признаков_меньше(self, config):
        """Иначе фиксированный порог означает «нужен идеальный максимум»."""
        cold = compute_score(features(), config)
        warm = compute_score(features(wallet_from_api=True), config)
        th = lambda s: max(config.score_threshold,
                           config.score_threshold_ratio * s.available_max)
        assert th(cold) < th(warm)


class TestColdStartBypass:
    def test_история_из_api_снимает_блокировку_признаков(self, config):
        """С history_days=0.1 локальные признаки выключены, но данные из API
        честные — значит новизна должна засчитаться."""
        f = features(is_new_wallet=True, wallet_from_api=True, history_days=0.1)
        assert compute_score(f, config).parts.get("wallet_new") == 15

    def test_без_api_на_свежей_базе_новизна_не_засчитывается(self, config):
        f = features(is_new_wallet=True, wallet_from_api=False, history_days=0.1)
        assert "wallet_new" not in compute_score(f, config).parts
