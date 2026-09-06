"""Имя трейдера в сообщении.

Polymarket подставляет в поле name строку вида
"0xC41D736b...-1777101352681" тем, кто имя не задавал. Показывать её
хуже, чем ничего: длинная, ничего не сообщает и ломает вид сообщения.
"""
from __future__ import annotations

from polymarket_tracker.telegram_notifier import clean_nickname


class TestCleanNickname:
    def test_заданное_имя_показываем(self):
        assert clean_nickname("ckryptoworker", "Humble-Socialism") == "ckryptoworker"

    def test_автоген_из_адреса_отбрасываем(self):
        auto = "0xC41D736bDed9ED1acCD6A44235039266219774fD-1777101352681"
        assert clean_nickname(auto, "Humble-Socialism") == "Humble-Socialism"

    def test_без_имени_берём_псевдоним(self):
        assert clean_nickname("", "Brave-Otter") == "Brave-Otter"

    def test_нет_ничего_молчим(self):
        assert clean_nickname("", "") == ""
        assert clean_nickname("   ", "  ") == ""

    def test_автоген_и_без_псевдонима_молчим(self):
        """Лучше без имени, чем адрес во второй раз: он и так в ссылке."""
        assert clean_nickname("0xabc123-999", "") == ""
