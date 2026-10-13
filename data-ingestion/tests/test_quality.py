"""
Тести common/quality.py — на фейковому conn/cursor (той самий
принцип, що test_watchlist_db.py), без реальної БД.
"""

from decimal import Decimal

from common.quality import _spread_pct, has_volatile_recent_revisions


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params):
        self.executed = (query, params)

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self.cursor_obj = _FakeCursor(rows)

    def cursor(self):
        return self.cursor_obj


def test_spread_pct_basic():
    assert _spread_pct("100", "102") == Decimal("2")


def test_spread_pct_zero_min_returns_zero_not_divide_error():
    assert _spread_pct("0", "5") == Decimal("0")


def test_has_volatile_recent_revisions_true_for_large_spread():
    # Живий кейс xauusd 2026-09-28: 4196.13 -> 4115.08, ~1.95% розмах.
    conn = _FakeConn(rows=[(Decimal("4115.08"), Decimal("4196.13"))])
    assert has_volatile_recent_revisions(conn, "twelvedata", "xauusd_close") is True


def test_has_volatile_recent_revisions_false_for_small_spread():
    # Звичайний шум округлення/нормальний внутрішньоденний рух.
    conn = _FakeConn(rows=[(Decimal("4137.52"), Decimal("4137.61"))])
    assert has_volatile_recent_revisions(conn, "twelvedata", "xauusd_close") is False


def test_has_volatile_recent_revisions_false_when_no_multi_revision_dates():
    conn = _FakeConn(rows=[])
    assert has_volatile_recent_revisions(conn, "tradingeconomics", "coffee") is False


def test_has_volatile_recent_revisions_true_if_any_single_date_exceeds():
    # Кілька дат — досить ОДНІЄЇ нестабільної, щоб прапорець спрацював.
    conn = _FakeConn(
        rows=[
            (Decimal("100"), Decimal("100.5")),  # стабільна дата
            (Decimal("4115.08"), Decimal("4196.13")),  # нестабільна дата
        ]
    )
    assert has_volatile_recent_revisions(conn, "twelvedata", "xauusd_close") is True


def test_has_volatile_recent_revisions_honors_custom_threshold():
    conn = _FakeConn(rows=[(Decimal("100"), Decimal("101"))])  # 1% розмах
    assert has_volatile_recent_revisions(conn, "src", "metric", threshold_pct=Decimal("2")) is False
    conn2 = _FakeConn(rows=[(Decimal("100"), Decimal("101"))])
    assert has_volatile_recent_revisions(conn2, "src", "metric", threshold_pct=Decimal("0.5")) is True
