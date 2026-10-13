"""
Глобальний (міжпроцесний) обмежувач швидкості — `common/rate_limit.py`.

Жива причина його появи (2026-10-05, docs/decisions.md): потоки новин
`general`/`watchlist` давали НУЛЬ статей, бо GDELT відповідав 429 уже
на перший запит — три джоби збору стартували в межах 5 хвилин, кожна
окремим процесом із власною `time.sleep()`, що нічого не знала про
решту.

Фейковий cursor/conn (той самий підхід, що test_watchlist_db.py) —
без реальної БД.
"""

from datetime import datetime, timedelta, timezone

from common.rate_limit import make_pacer, throttle


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params=None):
        self._conn.executed.append((" ".join(query.split()), params))
        self._last = query

    def fetchone(self):
        if "last_request_at FROM api_rate_limit" in self._last:
            return (self._conn.last_request_at,)
        return (None,)


class _FakeConn:
    def __init__(self, last_request_at=None):
        self.executed = []
        self.committed = 0
        self.last_request_at = last_request_at

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed += 1


def test_waits_remaining_interval():
    conn = _FakeConn(last_request_at=datetime.now(timezone.utc) - timedelta(seconds=2))
    slept = []

    waited = throttle(conn, "gdelt", min_interval_seconds=6.0, max_wait_seconds=60.0, sleep=slept.append)

    assert 3.5 <= waited <= 4.5
    assert len(slept) == 1


def test_does_not_wait_when_interval_passed():
    conn = _FakeConn(last_request_at=datetime.now(timezone.utc) - timedelta(seconds=30))
    slept = []

    assert throttle(conn, "gdelt", 6.0, 60.0, sleep=slept.append) == 0.0
    assert slept == []


def test_first_ever_request_does_not_wait():
    conn = _FakeConn(last_request_at=None)
    assert throttle(conn, "gdelt", 6.0, 60.0, sleep=lambda _: None) == 0.0


def test_wait_is_capped():
    """Зсунутий годинник чи рядок із майбутнім часом не має права
    підвісити джобу назавжди."""
    conn = _FakeConn(last_request_at=datetime.now(timezone.utc) + timedelta(hours=5))
    assert throttle(conn, "gdelt", 6.0, max_wait_seconds=60.0, sleep=lambda _: None) == 60.0


def test_takes_advisory_lock_and_records_request():
    """Серіалізація між ПРОЦЕСАМИ тримається саме на advisory-локі —
    без нього обмежувач вироджується в ту саму локальну паузу, що й
    не працювала."""
    conn = _FakeConn()
    throttle(conn, "gdelt", 6.0, 60.0, sleep=lambda _: None)

    queries = [q for q, _ in conn.executed]
    assert any("pg_advisory_xact_lock" in q for q in queries)
    assert any("INSERT INTO api_rate_limit" in q for q in queries)
    # commit звільняє транзакційний лок — без нього наступний процес
    # чекав би до кінця сесії.
    assert conn.committed == 1


def test_make_pacer_returns_zero_arg_callable():
    conn = _FakeConn()
    pacer = make_pacer(conn, "gdelt", 6.0, 60.0, sleep=lambda _: None)
    pacer()
    assert any("api_rate_limit" in q for q, _ in conn.executed)
