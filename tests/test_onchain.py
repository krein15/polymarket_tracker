"""Расшифровка событий OrderFilled из блокчейна.

Фикстуры — настоящие логи, снятые с контракта CTF Exchange V2, а не
придуманные. Расшифровка сверена с Data API по шести сделкам: сумма,
цена и токен совпали во всех случаях.

Смысл всей ветки: голова цепочки отстаёт от реального времени на ~1 с,
а Data API — на 250-380 с. В группе "рынок побежал" за первые пять минут
уходит четверть движения, то есть фора в пять минут стоит около 16% цены.
"""
from __future__ import annotations

import json
import pathlib

from polymarket_tracker.onchain_listener import DECIMALS, decode_order_filled

FIXTURES = json.loads(
    (pathlib.Path(__file__).parent / "fixtures_onchain.json").read_text(encoding="utf-8")
)


class TestDecode:
    def test_покупка_расшифровывается(self):
        t = decode_order_filled(FIXTURES["buy"])
        assert t is not None
        assert t.side == "buy"
        assert 0 < t.price <= 1
        assert t.usdc_amount > 0
        assert t.maker.startswith("0x") and len(t.maker) == 42

    def test_продажа_расшифровывается(self):
        t = decode_order_filled(FIXTURES["sell"])
        assert t is not None
        assert t.side == "sell"
        assert 0 < t.price <= 1

    def test_цена_согласована_с_суммами(self):
        """Цена не берётся готовой из события — она считается из двух сумм,
        и это надо проверять: перепутанные местами суммы дали бы 1/price."""
        t = decode_order_filled(FIXTURES["buy"])
        assert abs(t.price - t.usdc_amount / t.shares) < 1e-9

    def test_чужое_событие_игнорируется(self):
        """У контракта три типа событий; разбирать надо только OrderFilled."""
        assert decode_order_filled(FIXTURES["other"]) is None

    def test_номер_блока_и_лога_разобраны(self):
        t = decode_order_filled(FIXTURES["buy"])
        assert t.block_number > 90_000_000
        assert t.log_index >= 0


def make_log(side: int, maker_amount: int, taker_amount: int, token_id: int = 42):
    words = [side, token_id, maker_amount, taker_amount, 0, 0, 0]
    return {
        "topics": [
            "0xd543adfd94" + "0" * 54,
            "0x" + "1" * 64,
            "0x" + "0" * 24 + "a" * 40,
            "0x" + "0" * 24 + "b" * 40,
        ],
        "data": "0x" + "".join(f"{w:064x}" for w in words),
        "blockNumber": "0x1", "logIndex": "0x0", "transactionHash": "0xdead",
    }


class TestDecodeEdges:
    def test_покупка_считает_цену_как_usdc_на_долю(self):
        t = decode_order_filled(make_log(side=0, maker_amount=50 * DECIMALS,
                                         taker_amount=100 * DECIMALS))
        assert t.side == "buy"
        assert abs(t.price - 0.5) < 1e-9
        assert abs(t.usdc_amount - 50) < 1e-9

    def test_продажа_переворачивает_суммы(self):
        """При SELL мейкер отдаёт доли и получает USDC — местами наоборот."""
        t = decode_order_filled(make_log(side=1, maker_amount=100 * DECIMALS,
                                         taker_amount=50 * DECIMALS))
        assert t.side == "sell"
        assert abs(t.price - 0.5) < 1e-9
        assert abs(t.usdc_amount - 50) < 1e-9

    def test_нулевое_исполнение_отбрасывается(self):
        assert decode_order_filled(make_log(0, 0, 100 * DECIMALS)) is None
        assert decode_order_filled(make_log(0, 100 * DECIMALS, 0)) is None

    def test_цена_дороже_доллара_отбрасывается(self):
        """Доля бинарного исхода не может стоить больше доллара: если так
        вышло, значит суммы разобраны неверно, и лучше пропустить."""
        assert decode_order_filled(make_log(0, 200 * DECIMALS, 100 * DECIMALS)) is None

    def test_короткие_данные_не_ломают(self):
        bad = make_log(0, DECIMALS, DECIMALS)
        bad["data"] = "0x" + "0" * 64
        assert decode_order_filled(bad) is None

    def test_событие_с_другим_числом_тем_игнорируется(self):
        bad = make_log(0, DECIMALS, DECIMALS)
        bad["topics"] = bad["topics"][:3]
        assert decode_order_filled(bad) is None
