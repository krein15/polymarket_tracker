"""Уровни whitelist: разбор метаданных, персональные пороги, watch без сигнала."""
from __future__ import annotations

from polymarket_tracker.watchlist import TIER_PASS, TIER_WATCH, Watchlist, WhitelistEntry

ADDR = "0x" + "a" * 40
ADDR2 = "0x" + "b" * 40


def write(tmp_path, *lines):
    p = tmp_path / "whitelist.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


class TestParsing:
    def test_старый_формат_читается_как_pass_без_личного_порога(self, tmp_path):
        """Файлы, собранные до появления уровней, должны продолжать работать."""
        w = Watchlist(write(tmp_path, f"{ADDR}  # SomeTrader"))
        e = w.get(ADDR)
        assert e.tier == TIER_PASS
        assert e.big_usdc == 0.0
        assert e.nickname == "SomeTrader"
        assert e.signals_on_its_own is True

    def test_разбирает_tier_и_big(self, tmp_path):
        w = Watchlist(write(
            tmp_path,
            f"{ADDR}  # Ник | tier=watch | pnl=$120,000 roi=7.1% | big=$1,250 | ок",
        ))
        e = w.get(ADDR)
        assert e.tier == TIER_WATCH
        assert e.big_usdc == 1250.0
        assert e.nickname == "Ник"
        assert e.signals_on_its_own is False

    def test_big_без_знака_доллара_и_запятых(self, tmp_path):
        w = Watchlist(write(tmp_path, f"{ADDR}  # X | big=900"))
        assert w.get(ADDR).big_usdc == 900.0

    def test_неизвестный_tier_считается_pass(self, tmp_path):
        """Опечатка в файле не должна молча выключать адрес."""
        w = Watchlist(write(tmp_path, f"{ADDR}  # X | tier=супер"))
        assert w.get(ADDR).tier == TIER_PASS

    def test_комментарии_и_пустые_строки_игнорируются(self, tmp_path):
        w = Watchlist(write(tmp_path, "# заголовок", "", f"{ADDR}  # A", f"{ADDR2}  # B"))
        assert len(w) == 2

    def test_считает_адреса_по_уровням(self, tmp_path):
        w = Watchlist(write(
            tmp_path,
            f"{ADDR}  # A | tier=pass",
            f"{ADDR2}  # B | tier=watch",
        ))
        assert w.count_by_tier() == {TIER_PASS: 1, TIER_WATCH: 1}

    def test_неизвестный_адрес_даёт_none(self, tmp_path):
        w = Watchlist(write(tmp_path, f"{ADDR}  # A"))
        assert w.get(ADDR2) is None
        assert w.is_whitelisted(ADDR2) is False


class TestHotReload:
    def test_правка_файла_подхватывается(self, tmp_path):
        path = write(tmp_path, f"{ADDR}  # A | tier=pass")
        w = Watchlist(path)
        assert w.get(ADDR).tier == TIER_PASS

        import os, time
        time.sleep(0.01)
        (tmp_path / "whitelist.txt").write_text(
            f"{ADDR}  # A | tier=watch\n", encoding="utf-8"
        )
        os.utime(path, (time.time() + 1, time.time() + 1))  # гарантируем смену mtime
        assert w.get(ADDR).tier == TIER_WATCH


class TestEntry:
    def test_watch_не_даёт_своего_сигнала(self):
        assert WhitelistEntry(ADDR, tier=TIER_WATCH).signals_on_its_own is False

    def test_pass_даёт(self):
        assert WhitelistEntry(ADDR, tier=TIER_PASS).signals_on_its_own is True
