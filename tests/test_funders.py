"""Распутывание источника финансирования кошелька.

Почему это надо проверять тестом, а не глазами
----------------------------------------------
Деньги приходят на кошелёк Polymarket чеканкой pUSD, и настоящий спонсор
виден только на шаг глубже — внутри чека транзакции, где лежат десятки
чужих переводов из одной пакетной транзакции релеера (в разобранной живой
транзакции их было 213). Отличить нужную ножку USDC от чужих можно
единственным способом — по совпадению суммы с чеканкой.

Ошибка здесь не падает и не видна в выводе: скрипт просто назовёт спонсором
случайный адрес из пакета, кластеры получатся выдуманными, а выводы —
уверенными и ложными. Отсюда проверки ниже.
"""
from __future__ import annotations

import pytest

from tools import analyze_funders as af

WALLET = "0x" + "a" * 40
SPONSOR = "0x" + "b" * 40
STRANGER = "0x" + "c" * 40
USDC = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"

AMOUNT = 500_000_000  # $500 в шести знаках


def transfer(token: str, src: str, value: int) -> dict:
    return {
        "address": token,
        "topics": [af.TRANSFER, "0x" + "0" * 24 + src[2:], "0x" + "0" * 24 + WALLET[2:]],
        "data": hex(value),
    }


def fake_api(mint_tx: dict, receipt: dict):
    """Двойник Etherscan: первый вызов — переводы pUSD, второй — чек."""
    def es(params, tries=3):
        if params.get("action") == "tokentx":
            return [mint_tx]
        if params.get("action") == "eth_getTransactionReceipt":
            return receipt
        return None
    return es


@pytest.fixture
def mint():
    return {"to": WALLET, "value": str(AMOUNT), "hash": "0xdead"}


class TestFunderOf:
    def test_ножка_usdc_находится_по_совпадению_суммы(self, monkeypatch, mint):
        receipt = {"logs": [
            transfer(USDC, STRANGER, AMOUNT * 3),   # чужой перевод в том же пакете
            transfer(USDC, SPONSOR, AMOUNT),
            transfer(USDC, STRANGER, 42),
        ]}
        monkeypatch.setattr(af, "es", fake_api(mint, receipt))
        assert af.funder_of(WALLET) == SPONSOR

    def test_сама_чеканка_не_считается_источником(self, monkeypatch, mint):
        """Перевод pUSD на ту же сумму лежит в том же чеке и совпадает по
        сумме идеально — если его не отбросить, спонсором станет шлюз."""
        receipt = {"logs": [
            transfer(af.PUSD, STRANGER, AMOUNT),
            transfer(USDC, SPONSOR, AMOUNT),
        ]}
        monkeypatch.setattr(af, "es", fake_api(mint, receipt))
        assert af.funder_of(WALLET) == SPONSOR

    def test_контракты_биржи_не_источник(self, monkeypatch, mint):
        """Возврат с биржи — это его же деньги, а не чужое финансирование."""
        exchange = "0x" + next(iter(af.EXCHANGES))[2:]
        receipt = {"logs": [transfer(USDC, exchange, AMOUNT)]}
        monkeypatch.setattr(af, "es", fake_api(mint, receipt))
        assert af.funder_of(WALLET) is None

    def test_без_совпадения_суммы_ничего_не_придумываем(self, monkeypatch, mint):
        """Лучше "источник не найден", чем случайный адрес из пакета."""
        receipt = {"logs": [transfer(USDC, STRANGER, AMOUNT * 2)]}
        monkeypatch.setattr(af, "es", fake_api(mint, receipt))
        assert af.funder_of(WALLET) is None

    def test_исходящий_перевод_не_путается_с_приходом(self, monkeypatch):
        """В tokentx лежат обе стороны; источник ищем только по приходу."""
        out = {"to": STRANGER, "value": str(AMOUNT), "hash": "0xbeef"}
        receipt = {"logs": [transfer(USDC, SPONSOR, AMOUNT)]}
        monkeypatch.setattr(af, "es", fake_api(out, receipt))
        assert af.funder_of(WALLET) is None

    def test_отказ_api_не_роняет_разбор(self, monkeypatch, mint):
        monkeypatch.setattr(af, "es", lambda params, tries=3: None)
        assert af.funder_of(WALLET) is None

    def test_допуск_на_комиссию_в_один_процент(self, monkeypatch, mint):
        """Суммы сходятся не до копейки: по дороге снимается комиссия."""
        receipt = {"logs": [transfer(USDC, SPONSOR, AMOUNT - AMOUNT // 200)]}
        monkeypatch.setattr(af, "es", fake_api(mint, receipt))
        assert af.funder_of(WALLET) == SPONSOR


class TestClassifyFunder:
    """Отличить связку кошельков от шлюза.

    Наивный счёт получателей в фиксированном окне провалился на живых
    данных: шлюз 0x4d97dcd9 залил 64 наших кошелька, а в последних 200 его
    переводах получателей было четыре. Тысяча его переводов укладывается в
    ноль часов — окно наблюдения оказалось короче минуты.
    """

    def _txs(self, funder, recipients, count, span_hours):
        step = int(span_hours * 3600 / max(count - 1, 1))
        return [{"from": funder, "to": f"0x{i % recipients:040x}",
                 "timeStamp": str(1_700_000_000 + i * step)}
                for i in range(count)]

    def test_поток_за_считанные_часы_это_шлюз(self, monkeypatch):
        """Даже если получателей в выборке мало: она уперлась в предел, и
        настоящее их число нам просто не видно."""
        txs = self._txs(SPONSOR, recipients=4, count=af.SAMPLE_SIZE, span_hours=0.0)
        monkeypatch.setattr(af, "es", lambda params, tries=3: txs)
        assert af.classify_funder(SPONSOR)["kind"] == "service"

    def test_редкий_кошелёк_с_парой_адресов_это_связка(self, monkeypatch):
        txs = self._txs(SPONSOR, recipients=3, count=40, span_hours=24 * 180)
        monkeypatch.setattr(af, "es", lambda params, tries=3: txs)
        info = af.classify_funder(SPONSOR)
        assert info["kind"] == "narrow"
        assert info["recipients"] == 3

    def test_много_получателей_это_шлюз_даже_за_годы(self, monkeypatch):
        txs = self._txs(SPONSOR, recipients=200, count=400, span_hours=24 * 300)
        monkeypatch.setattr(af, "es", lambda params, tries=3: txs)
        assert af.classify_funder(SPONSOR)["kind"] == "service"

    def test_считаются_только_свои_отправки(self, monkeypatch):
        """У адреса в выборке есть и входящие переводы: если считать их,
        любой кошелёк выглядит широким шлюзом."""
        txs = [
            {"from": SPONSOR, "to": WALLET, "timeStamp": "1700000000"},
            {"from": SPONSOR, "to": STRANGER, "timeStamp": "1700100000"},
            {"from": SPONSOR, "to": WALLET, "timeStamp": "1700200000"},
            {"from": STRANGER, "to": SPONSOR, "timeStamp": "1700300000"},
        ]
        monkeypatch.setattr(af, "es", lambda params, tries=3: txs)
        assert af.classify_funder(SPONSOR)["recipients"] == 2

    def test_отказ_api_не_выдаётся_за_узкий_источник(self, monkeypatch):
        """Ноль получателей означал бы "связка" — а мы просто не смогли
        спросить. Такие адреса в узкие попадать не должны."""
        monkeypatch.setattr(af, "es", lambda params, tries=3: None)
        assert af.classify_funder(SPONSOR)["kind"] == "unknown"


class TestWilson:
    def test_интервал_накрывает_долю(self):
        lo, hi = af.wilson(90, 100)
        assert lo < 0.90 < hi

    def test_малая_выборка_даёт_широкий_интервал(self):
        narrow = af.wilson(900, 1000)
        wide = af.wilson(9, 10)
        assert (wide[1] - wide[0]) > (narrow[1] - narrow[0]) * 5

    def test_пустая_выборка_не_делит_на_ноль(self):
        assert af.wilson(0, 0) == (0.0, 0.0)
