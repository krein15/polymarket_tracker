"""Надзиратель: когда перезапускать, когда ждать дольше, когда сдаться.

Почему это вообще проверяется тестами
------------------------------------
09-10.09 трекер простоял сутки: `.bat` запускал его один раз, процесс умер,
и всё встало до ручного вмешательства. Теперь простой стоит времени — чтобы
отличить прибыль от нуля, нужно порядка 700 закрывшихся сделок, около
месяца, и каждые сутки простоя добавляются к сроку.

Две ошибки, которые здесь легко сделать и трудно заметить:
  * вечный цикл на сломанном трекере — надзиратель "работает", данных нет;
  * сброс счётчика при любом успехе — процесс, падающий раз в минуту,
    выглядел бы здоровым.
"""
from __future__ import annotations

import tools.supervise as sup
from tools.supervise import (
    BACKOFF_SEC,
    GIVE_UP_AFTER,
    STABLE_RUN_SEC,
    next_delay,
    run_was_stable,
    should_give_up,
)


class TestBackoff:
    def test_первая_попытка_быстрая(self):
        """Обычный обрыв сети лечится сам за секунды — не тянем."""
        assert next_delay(1) == BACKOFF_SEC[0]
        assert next_delay(1) <= 30

    def test_пауза_растёт(self):
        delays = [next_delay(i) for i in range(1, len(BACKOFF_SEC) + 1)]
        assert delays == sorted(delays)
        assert delays[-1] > delays[0]

    def test_пауза_не_растёт_бесконечно(self):
        assert next_delay(100) == BACKOFF_SEC[-1]

    def test_без_неудач_не_ждём(self):
        assert next_delay(0) == 0


class TestStability:
    def test_долгая_работа_считается_нормальной(self):
        assert run_was_stable(STABLE_RUN_SEC)
        assert run_was_stable(STABLE_RUN_SEC + 1)

    def test_мгновенное_падение_не_считается(self):
        assert not run_was_stable(2)
        assert not run_was_stable(STABLE_RUN_SEC - 1)


class TestGiveUp:
    def test_сдаёмся_после_череды_падений(self):
        """Иначе надзиратель бесконечно скрывает поломку: процесс вроде бы
        перезапускается, а данных не прибавляется."""
        assert should_give_up(GIVE_UP_AFTER)
        assert should_give_up(GIVE_UP_AFTER + 5)

    def test_единичные_падения_терпим(self):
        assert not should_give_up(1)
        assert not should_give_up(GIVE_UP_AFTER - 1)

    def test_порог_не_единица(self):
        """Сдаваться после первого же падения — значит не иметь надзирателя."""
        assert GIVE_UP_AFTER > 1


class TestLoop:
    """Сам цикл: главное — что он конечен на сломанном трекере."""

    def _fake(self, monkeypatch, exit_codes, durations):
        calls = {"n": 0}
        clock = {"t": 1000.0}

        def fake_call(cmd, cwd=None):
            i = calls["n"]
            calls["n"] += 1
            clock["t"] += durations[min(i, len(durations) - 1)]
            return exit_codes[min(i, len(exit_codes) - 1)]

        monkeypatch.setattr(sup.subprocess, "call", fake_call)
        monkeypatch.setattr(sup.time, "time", lambda: clock["t"])
        monkeypatch.setattr(sup.time, "sleep", lambda s: None)
        monkeypatch.setattr(sup, "log", lambda m: None)
        return calls

    def test_сдаётся_на_вечно_падающем(self, monkeypatch):
        calls = self._fake(monkeypatch, [1], [0.5])
        assert sup.main() == 1
        assert calls["n"] == sup.GIVE_UP_AFTER

    def test_штатный_выход_не_перезапускает(self, monkeypatch):
        """Остановили руками — значит остановили, а не 'упал'."""
        calls = self._fake(monkeypatch, [0], [60.0])
        assert sup.main() == 0
        assert calls["n"] == 1

    def test_после_долгой_работы_поднимает_бесконечно(self, monkeypatch):
        """Трекер, честно проживший полчаса и упавший, должен подниматься
        всегда — даже если так повторится сто раз за месяц. Иначе однажды
        он просто не встанет, и мы снова узнаем об этом из логов.

        Проверяем именно это: счётчик неудач не копится, предел сдачи не
        достигается, цикл прерывается только внешней остановкой.
        """
        calls = {"n": 0}
        clock = {"t": 1000.0}
        LIMIT = sup.GIVE_UP_AFTER * 3

        def fake_call(cmd, cwd=None):
            calls["n"] += 1
            if calls["n"] > LIMIT:
                raise KeyboardInterrupt
            clock["t"] += sup.STABLE_RUN_SEC + 10
            return 1

        monkeypatch.setattr(sup.subprocess, "call", fake_call)
        monkeypatch.setattr(sup.time, "time", lambda: clock["t"])
        monkeypatch.setattr(sup.time, "sleep", lambda s: None)
        monkeypatch.setattr(sup, "log", lambda m: None)

        assert sup.main() == 0            # прервали снаружи, а не сдались
        assert calls["n"] > sup.GIVE_UP_AFTER

    def test_короткие_падения_копятся_даже_после_долгой_работы(self, monkeypatch):
        """Обратная сторона: если после нормального запуска трекер начал
        падать мгновенно — это поломка, и сдаться надо."""
        calls = {"n": 0}
        clock = {"t": 1000.0}

        def fake_call(cmd, cwd=None):
            calls["n"] += 1
            clock["t"] += (sup.STABLE_RUN_SEC + 10) if calls["n"] == 1 else 0.5
            return 1

        monkeypatch.setattr(sup.subprocess, "call", fake_call)
        monkeypatch.setattr(sup.time, "time", lambda: clock["t"])
        monkeypatch.setattr(sup.time, "sleep", lambda s: None)
        monkeypatch.setattr(sup, "log", lambda m: None)

        assert sup.main() == 1
        assert calls["n"] == sup.GIVE_UP_AFTER
