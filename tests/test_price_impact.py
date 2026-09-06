"""Удар по цене: переплатил ли трейдер относительно недавнего рынка.

Замер на 2219 теневых сделках, который и породил признак:
    переплата > +20%  -> дрейф через час +17.6%, перевес +9.9 пп (n=117)
    около нуля        -> дрейф  +0.1%,           перевес -1.8 пп (n=1710)
    вход НИЖЕ рынка   -> дрейф -23.5%,           перевес -10.3 пп (n=148)
Контроль на размер: внутри корзины $1k-5k переплатившие дали +14.9% дрейфа
против -2.5%, а медианный размер у них даже меньше. Значит, это не размер
в маскировке.
"""
from __future__ import annotations

from conftest import NOW

from polymarket_tracker.scoring import PRICE_IMPACT_WINDOW_SEC


def buy(storage, price, ts, token_id="token-1", maker=None, usdc=500.0):
    maker = maker or ("0x" + "1" * 40)
    storage.save_trade(
        tx_hash=f"0x{price}{ts}{maker[-4:]}", log_index=0, ts=ts, block_number=0,
        maker=maker, token_id=token_id, side="buy", usdc_amount=usdc,
        price=price, condition_id="0xc",
    )


class TestReferencePrice:
    def test_медиана_недавних_покупок(self, storage):
        for i, p in enumerate((0.40, 0.50, 0.60)):
            buy(storage, p, NOW - 100 - i)
        ref = storage.market_reference_price("token-1", NOW, PRICE_IMPACT_WINDOW_SEC)
        assert ref == 0.50

    def test_мало_сделок_опоры_нет(self, storage):
        """По двум точкам опору не строят — признак должен молчать."""
        buy(storage, 0.50, NOW - 10)
        buy(storage, 0.55, NOW - 20)
        assert storage.market_reference_price("token-1", NOW, PRICE_IMPACT_WINDOW_SEC) is None

    def test_старые_сделки_не_учитываются(self, storage):
        for i in range(5):
            buy(storage, 0.90, NOW - PRICE_IMPACT_WINDOW_SEC - 100 - i)
        assert storage.market_reference_price("token-1", NOW, PRICE_IMPACT_WINDOW_SEC) is None

    def test_будущие_сделки_не_учитываются(self, storage):
        """Опора строится строго ДО сделки, иначе признак заглядывает вперёд."""
        for i in range(5):
            buy(storage, 0.90, NOW + 100 + i)
        for i in range(3):
            buy(storage, 0.30, NOW - 100 - i)
        assert storage.market_reference_price("token-1", NOW, PRICE_IMPACT_WINDOW_SEC) == 0.30

    def test_чужой_исход_не_мешает(self, storage):
        for i in range(4):
            buy(storage, 0.90, NOW - 50 - i, token_id="token-other")
        for i in range(3):
            buy(storage, 0.20, NOW - 50 - i)
        assert storage.market_reference_price("token-1", NOW, PRICE_IMPACT_WINDOW_SEC) == 0.20

    def test_продажи_не_учитываются(self, storage):
        for i in range(4):
            storage.save_trade(
                tx_hash=f"0xs{i}", log_index=0, ts=NOW - 50 - i, block_number=0,
                maker="0x" + "2" * 40, token_id="token-1", side="sell",
                usdc_amount=100.0, price=0.90, condition_id="0xc",
            )
        assert storage.market_reference_price("token-1", NOW, PRICE_IMPACT_WINDOW_SEC) is None
