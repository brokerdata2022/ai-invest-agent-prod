"""
Тести main.py:check_startup_gap() — на фейковому з'єднанні (той самий
принцип, що test_scheduler_heartbeat.py/test_runner.py): monkeypatch
модульних функцій (`main.get_connection`/`main.notify_scheduler_gap`),
без реальної БД і без реальної Telegram-відправки.

Живий випадок, що ця логіка закриває (2026-10-02): реліз NFP/Unemployment
о 15:30 опрацьовано лише о 17:57 — причина була не в конвеєрі (~15 хв),
а в непоміченому ~11-год простої scheduler. check_startup_gap() має
відрізняти ТАКИЙ розрив від нормального (короткий docker compose
restart, пропущений один тик) — пороги саме для цього.
"""

from datetime import datetime, timedelta, timezone

import main


class _FakeCursor:
    def __init__(self, row) -> None:
        self._row = row

    def execute(self, *args, **kwargs) -> None:
        pass

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeConn:
    def __init__(self, row) -> None:
        self._row = row
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._row)

    def close(self) -> None:
        self.closed = True


def _patch_gap_alert(monkeypatch):
    calls: list = []
    monkeypatch.setattr(main, "notify_scheduler_gap", lambda *a: calls.append(a))
    return calls


def test_no_alert_when_heartbeat_never_recorded(monkeypatch):
    """Перший запуск узагалі (свіжа БД) — нема з чим звіряти, не падати
    й не слати хибний алерт."""
    monkeypatch.setattr(main, "get_connection", lambda: _FakeConn(None))
    calls = _patch_gap_alert(monkeypatch)

    main.check_startup_gap()

    assert calls == []


def test_no_alert_for_gap_within_threshold(monkeypatch):
    """Звичайний docker compose restart (секунди) чи один пропущений
    5-хв тик — у межах норми, не варте алерту."""
    now = datetime.now(timezone.utc)
    last_tick_at = now - timedelta(minutes=10)
    monkeypatch.setattr(main, "get_connection", lambda: _FakeConn((last_tick_at,)))
    calls = _patch_gap_alert(monkeypatch)

    main.check_startup_gap()

    assert calls == []


def test_alert_for_gap_beyond_threshold(monkeypatch):
    """Регресія 2026-10-02: ~11-год простій (контейнер/хост не
    піднятий) ПОВИНЕН тригернути рівно один алерт із правильними
    часами/розривом."""
    now = datetime.now(timezone.utc)
    last_tick_at = now - timedelta(hours=11)
    monkeypatch.setattr(main, "get_connection", lambda: _FakeConn((last_tick_at,)))
    calls = _patch_gap_alert(monkeypatch)

    main.check_startup_gap()

    assert len(calls) == 1
    last_seen, resumed_at, gap = calls[0]
    assert last_seen == last_tick_at
    assert gap > timedelta(hours=10)


def test_connection_is_closed_even_without_alert(monkeypatch):
    now = datetime.now(timezone.utc)
    fake_conn = _FakeConn((now,))
    monkeypatch.setattr(main, "get_connection", lambda: fake_conn)
    _patch_gap_alert(monkeypatch)

    main.check_startup_gap()

    assert fake_conn.closed is True
