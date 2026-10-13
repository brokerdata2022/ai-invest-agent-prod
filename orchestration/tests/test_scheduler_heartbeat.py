"""
Тести jobs.py:_scheduler_heartbeat() — на фейковому з'єднанні, без
реальної БД (той самий принцип, що test_prune_logs.py: ізоляція від
робочого середовища). Перевіряє лише, що UPSERT і commit/close дійсно
викликані — саму логіку часу (розрив/поріг) перевіряє test_main.py.
"""

import common.db
import jobs


class _FakeCursor:
    def __init__(self, log: list) -> None:
        self._log = log

    def execute(self, sql, params=None) -> None:
        self._log.append((" ".join(sql.split()), params))

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeConn:
    def __init__(self) -> None:
        self.log: list = []
        self.committed = False
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.log)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def test_upserts_heartbeat_row_and_commits(monkeypatch):
    fake_conn = _FakeConn()
    monkeypatch.setattr(common.db, "get_connection", lambda: fake_conn)

    jobs._scheduler_heartbeat()

    assert fake_conn.committed is True
    assert fake_conn.closed is True
    assert len(fake_conn.log) == 1
    sql, params = fake_conn.log[0]
    assert "INSERT INTO scheduler_heartbeat" in sql
    assert "ON CONFLICT (id) DO UPDATE" in sql


def test_closes_connection_even_if_execute_fails(monkeypatch):
    class _FailingCursor(_FakeCursor):
        def execute(self, sql, params=None) -> None:
            raise RuntimeError("бум")

    class _FailingConn(_FakeConn):
        def cursor(self):
            return _FailingCursor(self.log)

    fake_conn = _FailingConn()
    monkeypatch.setattr(common.db, "get_connection", lambda: fake_conn)

    try:
        jobs._scheduler_heartbeat()
    except RuntimeError:
        pass

    assert fake_conn.closed is True
