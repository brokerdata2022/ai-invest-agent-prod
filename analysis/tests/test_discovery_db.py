"""
SQL-шар пошуку новачків (`discovery/_db.py`) — фейковий cursor/conn,
без реальної БД (той самий підхід, що test_news_analysis_db.py).

Два тести тут не косметичні:
1. `notified_at = NULL` в UPSERT — та сама регресія 2026-09-29, що й
   у синтезів: без скидання повторний прогін того самого дня з НОВИМИ
   числами мовчки не доходив би в Telegram (а прогонів тепер три на
   добу, не один).
2. Нові колонки (asset_class/direction/detector/score/signals) справді
   пишуться — інакше повідомлення показувало б напрямок за
   замовчуванням ("unclear") для всіх.
"""

import json
from decimal import Decimal

from discovery._db import fetch_tracked_identifiers, save_candidate
from discovery.signals import Candidate


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params=None):
        self._conn.executed.append((query, params))

    def fetchone(self):
        return (1,)

    def fetchall(self):
        return self._conn.rows.pop(0) if self._conn.rows else []


class _FakeConn:
    def __init__(self, rows=None):
        self.executed = []
        self.committed = 0
        self.rows = rows or []

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed += 1


def _candidate():
    return Candidate(
        asset_id="XUSDT",
        label="X/USDT (ф'ючерс)",
        asset_class="crypto",
        detector="crypto_movers",
        direction="down",
        reason="рух -19.0% за 24г",
        signals={"move_pct_24h": Decimal("-19")},
        score=Decimal("0.2850"),
    )


def test_save_candidate_upsert_resets_notified_at():
    conn = _FakeConn()
    assert save_candidate(conn, _candidate()) == 1
    assert conn.committed == 1

    query, _ = conn.executed[0]
    set_clause = query.split("DO UPDATE SET", 1)[1]
    assert "notified_at = NULL" in set_clause


def test_save_candidate_writes_new_columns():
    conn = _FakeConn()
    save_candidate(conn, _candidate())
    query, params = conn.executed[0]

    for column in ("asset_class", "direction", "detector", "score", "signals"):
        assert column in query

    assert "crypto" in params and "down" in params and "crypto_movers" in params
    # signals — JSON-рядок (Decimal сериалізується через default=str,
    # інакше json.dumps падає на Decimal і валить увесь прогін).
    signals = json.loads([p for p in params if isinstance(p, str) and p.startswith("{")][0])
    assert signals["move_pct_24h"] == "-19"


def test_tracked_identifiers_normalizes_watchlist_shapes():
    """Watchlist тримає ТРИ різні форми того самого активу
    (asset_id 'eurusd', ticker 'EUR/USD', metric_id 'eurusd_close') —
    усі три мусять виключати актив із "новачків", інакше він приходив
    би як новина про сам себе."""
    conn = _FakeConn(
        rows=[
            [("eurusd", "EUR/USD", "eurusd_close"), ("btc", None, "btc_close")],
            [("PEPEUSDT",)],
            [("WIFUSDT",)],
            [("AAPL",)],
        ]
    )

    tracked = fetch_tracked_identifiers(conn)

    assert {"EURUSD", "BTC", "PEPEUSDT", "WIFUSDT", "AAPL"} <= tracked
