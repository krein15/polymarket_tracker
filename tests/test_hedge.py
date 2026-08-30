"""Хедж: покупка ОБОИХ исходов рынка не должна давать сигнал.

Отличие от wallet_traded_both_sides: там про вход и выход по одному токену,
здесь про ставку на оба результата. У YES и NO разные token_id, поэтому
единственный способ их связать — общий condition_id.
"""
from __future__ import annotations

from conftest import NOW, make_market, make_trade, make_wallet
from test_detector import FakeWatchlist, detector

from polymarket_tracker.anomaly_detector import AnomalyDetector
from polymarket_tracker.watchlist import WhitelistEntry

MAKER = "0x" + "e" * 40
COND = "0xcondition"
YES, NO = "token-yes", "token-no"


def buy(storage, token_id, condition_id=COND, maker=MAKER, usdc=5000.0, ts=NOW):
    storage.save_trade(
        tx_hash=f"0x{token_id}{ts}", log_index=0, ts=ts, block_number=0,
        maker=maker, token_id=token_id, side="buy",
        usdc_amount=usdc, price=0.5, condition_id=condition_id,
    )


class TestStorage:
    def test_один_исход_не_хедж(self, storage):
        buy(storage, YES)
        assert storage.wallet_bought_both_outcomes(MAKER, COND, NOW - 3600) is False

    def test_оба_исхода_это_хедж(self, storage):
        buy(storage, YES)
        buy(storage, NO)
        assert storage.wallet_bought_both_outcomes(MAKER, COND, NOW - 3600) is True

    def test_разные_рынки_не_путаются(self, storage):
        buy(storage, YES, condition_id="0xmarket-1")
        buy(storage, NO, condition_id="0xmarket-2")
        assert storage.wallet_bought_both_outcomes(MAKER, "0xmarket-1", NOW - 3600) is False

    def test_чужой_кошелёк_не_считается(self, storage):
        buy(storage, YES)
        buy(storage, NO, maker="0x" + "d" * 40)
        assert storage.wallet_bought_both_outcomes(MAKER, COND, NOW - 3600) is False

    def test_окно_учитывается(self, storage):
        """Покупка второго исхода год назад — не хедж, а смена мнения."""
        buy(storage, YES, ts=NOW)
        buy(storage, NO, ts=NOW - 400 * 86400)
        assert storage.wallet_bought_both_outcomes(MAKER, COND, NOW - 3600) is False

    def test_продажа_второго_исхода_не_хедж(self, storage):
        """Хедж — это купить оба, а не купить один и закрыть другой."""
        buy(storage, YES)
        storage.save_trade(
            tx_hash="0xsell", log_index=0, ts=NOW, block_number=0, maker=MAKER,
            token_id=NO, side="sell", usdc_amount=100.0, price=0.5, condition_id=COND,
        )
        assert storage.wallet_bought_both_outcomes(MAKER, COND, NOW - 3600) is False

    def test_пустой_condition_id_не_ломает(self, storage):
        """Записи до миграции: судить не по чему, ложно срабатывать нельзя."""
        buy(storage, YES, condition_id="")
        assert storage.wallet_bought_both_outcomes(MAKER, "", NOW - 3600) is False


class TestBranchB:
    """Whitelist-сигнал раньше смотрел только на сумму — хедж проходил насквозь."""

    def _evaluate(self, config, storage, trade):
        entry = WhitelistEntry(MAKER, tier="pass", big_usdc=0.0)
        d = AnomalyDetector(config, storage, FakeWatchlist([entry]))
        return d.evaluate(trade, make_market(), make_wallet())

    def test_обычная_покупка_даёт_сигнал(self, config, storage):
        t = make_trade(maker=MAKER, usdc=5000, token_id=YES, condition_id=COND)
        buy(storage, YES)
        res = self._evaluate(config, storage, t)
        assert [s.signal_type for s in res.signals if s.signal_type == "whitelist"]

    def test_хедж_сигнал_не_даёт(self, config, storage):
        buy(storage, YES)
        buy(storage, NO)
        t = make_trade(maker=MAKER, usdc=5000, token_id=NO, condition_id=COND)
        res = self._evaluate(config, storage, t)
        assert [s for s in res.signals if s.signal_type == "whitelist"] == []

    def test_хедж_штрафуется_в_скоринге(self, config, storage):
        buy(storage, YES)
        buy(storage, NO)
        t = make_trade(maker=MAKER, usdc=9000, token_id=NO, condition_id=COND)
        res = self._evaluate(config, storage, t)
        assert res.score.parts.get("hedge") == -60
