"""Запросы-признаки в Storage: накопление, пробуждение, кластер, маркет-мейкер."""
from __future__ import annotations

from conftest import NOW, make_trade


def _save(storage, **kw):
    t = make_trade(**kw)
    storage.save_trade(
        tx_hash=t.tx_hash, log_index=0, ts=t.timestamp, block_number=0,
        maker=t.maker, token_id=t.token_id, side=t.side,
        usdc_amount=t.usdc_amount, price=t.price,
    )
    return t


class TestAccumulation:
    def test_суммирует_только_покупки_в_окне(self, storage):
        maker = "0x" + "a" * 40
        _save(storage, maker=maker, usdc=300, ts=NOW - 600, tx_hash="0x1")
        _save(storage, maker=maker, usdc=400, ts=NOW - 300, tx_hash="0x2")
        _save(storage, maker=maker, usdc=500, ts=NOW, tx_hash="0x3")
        # вне окна
        _save(storage, maker=maker, usdc=9000, ts=NOW - 5000, tx_hash="0x4")
        # продажа не должна попасть в набор позиции
        _save(storage, maker=maker, usdc=1000, ts=NOW, side="sell", tx_hash="0x5")

        total, n = storage.sum_wallet_buys_for_token(maker, "token-1", NOW - 1800)
        assert total == 1200.0
        assert n == 3

    def test_чужие_кошельки_не_считаются(self, storage):
        _save(storage, maker="0x" + "a" * 40, usdc=300, tx_hash="0x1")
        _save(storage, maker="0x" + "b" * 40, usdc=900, tx_hash="0x2")
        total, n = storage.sum_wallet_buys_for_token("0x" + "a" * 40, "token-1", NOW - 1800)
        assert (total, n) == (300.0, 1)

    def test_пустая_история_даёт_нули(self, storage):
        assert storage.sum_wallet_buys_for_token("0x" + "c" * 40, "token-1", 0) == (0.0, 0)


class TestDormancy:
    def test_возвращает_предыдущую_сделку_строго_до_текущей(self, storage):
        maker = "0x" + "d" * 40
        _save(storage, maker=maker, ts=NOW - 90 * 86400, tx_hash="0x1")
        _save(storage, maker=maker, ts=NOW, tx_hash="0x2")
        prev = storage.wallet_prev_trade_ts(maker, NOW)
        assert prev == NOW - 90 * 86400

    def test_первая_сделка_кошелька_даёт_none(self, storage):
        maker = "0x" + "e" * 40
        _save(storage, maker=maker, ts=NOW, tx_hash="0x1")
        assert storage.wallet_prev_trade_ts(maker, NOW) is None


class TestCluster:
    def test_считает_разные_кошельки_независимо_от_новизны(self, storage):
        for i, letter in enumerate("abcd"):
            _save(storage, maker="0x" + letter * 40, ts=NOW - i * 60, tx_hash=f"0x{i}")
        assert storage.count_distinct_wallets_for_token("token-1", NOW - 3600) == 4

    def test_за_окном_не_считает(self, storage):
        _save(storage, maker="0x" + "a" * 40, ts=NOW - 7200, tx_hash="0x1")
        _save(storage, maker="0x" + "b" * 40, ts=NOW, tx_hash="0x2")
        assert storage.count_distinct_wallets_for_token("token-1", NOW - 3600) == 1


class TestMarketMaker:
    def test_обе_стороны_рынка_опознаются(self, storage):
        maker = "0x" + "f" * 40
        _save(storage, maker=maker, side="buy", tx_hash="0x1")
        _save(storage, maker=maker, side="sell", tx_hash="0x2")
        assert storage.wallet_traded_both_sides(maker, "token-1") is True

    def test_односторонняя_торговля_не_маркет_мейкер(self, storage):
        maker = "0x" + "9" * 40
        _save(storage, maker=maker, side="buy", tx_hash="0x1")
        _save(storage, maker=maker, side="buy", ts=NOW - 60, tx_hash="0x2")
        assert storage.wallet_traded_both_sides(maker, "token-1") is False


class TestBaseline:
    def test_мало_истории_даёт_none(self, storage):
        """На свежей БД базовый оборот считать не из чего — честный None,
        а не случайное число (вызывающий код падает на volume24h из Gamma)."""
        _save(storage, ts=NOW - 600, tx_hash="0x1")
        assert storage.market_hourly_baseline("token-1", NOW, 168, 6) is None

    def test_считает_средний_часовой_оборот(self, storage):
        # 10 часов истории по $100 в час
        for h in range(10):
            _save(storage, usdc=100.0, ts=NOW - h * 3600, tx_hash=f"0x{h}")
        base = storage.market_hourly_baseline("token-1", NOW, 168, 6)
        assert base is not None
        assert 90 <= base <= 120
