"""Ограничители потока сигналов.

Замер на живых данных: 20.4 сигнала в час, медианный размер сделки $575,
и 82% сигналов держались на признаке "кластер". Разбор показал, что кластер
считал НОВЫЕ кошельки по локальному trade_count, а на молодой базе 86%
адресов имеют меньше 20 сделок — то есть признак означал просто "несколько
участников" и срабатывал почти везде.
"""
from __future__ import annotations

from conftest import NOW, make_market, make_trade, make_wallet
from test_detector import detector, save

MAKER = "0x" + "7" * 40
TOKEN = "token-1"


def buy(storage, maker, usdc, ts=NOW, token_id=TOKEN):
    storage.save_trade(
        tx_hash=f"0x{maker[-6:]}{ts}{int(usdc)}", log_index=0, ts=ts, block_number=0,
        maker=maker, token_id=token_id, side="buy", usdc_amount=usdc,
        price=0.5, condition_id="0xcond",
    )


class TestClusterByMoney:
    """Кластер должен считать деньги, а не «сколько раз мы видели адрес»."""

    def test_мелкие_участники_кластер_не_образуют(self, storage, config):
        for i in range(6):
            buy(storage, f"0x{i:040x}", usdc=50.0)
        n = storage.count_cluster_participants(TOKEN, NOW - 3600, min_usdc=500.0)
        assert n == 0

    def test_крупные_участники_считаются(self, storage):
        for i in range(4):
            buy(storage, f"0x{i:040x}", usdc=900.0)
        assert storage.count_cluster_participants(TOKEN, NOW - 3600, 500.0) == 4

    def test_один_кошелёк_не_толпа(self, storage):
        """Тот же адрес пятью покупками — это накопление, а не кластер."""
        for k in range(5):
            buy(storage, MAKER, usdc=900.0, ts=NOW - k * 60)
        assert storage.count_cluster_participants(TOKEN, NOW - 3600, 500.0) == 1

    def test_окно_учитывается(self, storage):
        buy(storage, "0x" + "1" * 40, usdc=900.0, ts=NOW)
        buy(storage, "0x" + "2" * 40, usdc=900.0, ts=NOW - 7200)
        assert storage.count_cluster_participants(TOKEN, NOW - 3600, 500.0) == 1

    def test_продажи_не_считаются(self, storage):
        buy(storage, "0x" + "1" * 40, usdc=900.0)
        storage.save_trade(
            tx_hash="0xs", log_index=0, ts=NOW, block_number=0, maker="0x" + "2" * 40,
            token_id=TOKEN, side="sell", usdc_amount=900.0, price=0.5, condition_id="0xc",
        )
        assert storage.count_cluster_participants(TOKEN, NOW - 3600, 500.0) == 1

    def test_другой_исход_не_считается(self, storage):
        buy(storage, "0x" + "1" * 40, usdc=900.0)
        buy(storage, "0x" + "2" * 40, usdc=900.0, token_id="token-other")
        assert storage.count_cluster_participants(TOKEN, NOW - 3600, 500.0) == 1


class TestSignalMoneyFloor:
    """Сигнал на $300 не действие, а шум — независимо от балла."""

    def _fire(self, config, storage, usdc, extra_buys=0):
        # набиваем кластер, чтобы балл заведомо взял порог
        for i in range(6):
            buy(storage, f"0x{i:040x}", usdc=2000.0)
        for k in range(extra_buys):
            buy(storage, MAKER, usdc=usdc, ts=NOW - (k + 1) * 60)
        t = make_trade(maker=MAKER, usdc=usdc, token_id=TOKEN)
        save(storage, t)
        res = detector(config, storage).evaluate(t, make_market(volume_24h=1000.0), make_wallet())
        return [s for s in res.signals if s.signal_type == "score"]

    def test_мелкая_сделка_не_шлётся(self, config, storage):
        config.signal_min_usdc = 5000.0
        assert self._fire(config, storage, usdc=300.0) == []

    def test_крупная_шлётся(self, config, storage):
        config.signal_min_usdc = 5000.0
        assert len(self._fire(config, storage, usdc=9000.0)) == 1

    def test_набор_частями_проходит_по_сумме(self, config, storage):
        """Двадцать покупок по $300 — это $6000 и как раз интересный случай:
        порог смотрит на накопленное, а не на последнюю покупку."""
        config.signal_min_usdc = 5000.0
        got = self._fire(config, storage, usdc=600.0, extra_buys=10)
        assert len(got) == 1
