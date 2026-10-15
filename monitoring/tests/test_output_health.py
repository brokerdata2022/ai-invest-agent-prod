"""
Тести детектора вироджених виводів (monitoring/output_health.py).

Перевіряються три живі дефекти, які він має ловити
(docs/decisions.md 2026-10-13/15), і два стани, які дефектами НЕ є:
порожня подієва таблиця й таблиця, що ще не починала працювати. Друге
важливо не менше за перше: детектор, який кричить на свіжому
розгортанні, вчить ігнорувати алерти.

БД тут не потрібна — підміняється курсор, бо перевіряється логіка
вердикту, а не SQL (той самий підхід, що в test_run_screening.py).
"""

import pytest

from output_health import FAIL, OK, SKIPPED, _safe_ident, run_check


class FakeCursor:
    """Віддає заздалегідь задані значення в порядку запитів:
    спершу агрегати перевірки, потім (якщо спитають) EXISTS."""

    def __init__(self, rows: list, columns: list):
        self._rows = list(rows)
        self._columns = list(columns)
        self.description = None
        self.queries: list = []

    def execute(self, query, params=None):
        self.queries.append(query)
        self._current = self._rows.pop(0)
        self.description = [(name,) for name in self._columns.pop(0)]

    def fetchone(self):
        return self._current

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _check(**overrides) -> dict:
    base = {
        "name": "demo",
        "table": "demo_table",
        "time_column": "created_at",
        "window_hours": 36,
        "min_rows": 1,
        "vary": ("cluster_count",),
        "min_distinct": 2,
        "required": ("summary",),
        "max_null_share": 0.0,
    }
    base.update(overrides)
    return base


def test_constant_column_is_degenerate():
    """Живий дефект 13.10: market_synthesis.cluster_count = 8 у ВСІХ
    рядках, бо пошук дублікатів не знаходив їх узагалі."""
    conn = FakeConn(FakeCursor(
        rows=[(8, 1, 0)],
        columns=[["rows", "distinct_cluster_count", "nulls_summary"]],
    ))
    result = run_check(conn, _check())

    assert result["status"] == FAIL
    assert "лише 1 різних значень" in result["detail"]


def test_varied_column_is_healthy():
    conn = FakeConn(FakeCursor(
        rows=[(8, 8, 0)],
        columns=[["rows", "distinct_cluster_count", "nulls_summary"]],
    ))
    assert run_check(conn, _check())["status"] == OK


def test_nulls_in_required_field_fail():
    conn = FakeConn(FakeCursor(
        rows=[(8, 5, 4)],
        columns=[["rows", "distinct_cluster_count", "nulls_summary"]],
    ))
    result = run_check(conn, _check())

    assert result["status"] == FAIL
    assert "NULL у 50%" in result["detail"]


def test_too_few_rows_fail():
    conn = FakeConn(FakeCursor(
        rows=[(3, 3, 0)],
        columns=[["rows", "distinct_cluster_count", "nulls_summary"]],
    ))
    result = run_check(conn, _check(min_rows=10))

    assert result["status"] == FAIL
    assert "рядків 3 < 10" in result["detail"]


def test_event_driven_empty_table_is_skipped_not_failed():
    """`min_rows: 0` — подієва таблиця (прогнози пишуться після
    релізів). Порожнеча тут норма."""
    conn = FakeConn(FakeCursor(
        rows=[(0, 0, 0)],
        columns=[["rows", "distinct_cluster_count", "nulls_summary"]],
    ))
    result = run_check(conn, _check(min_rows=0))

    assert result["status"] == SKIPPED


def test_never_used_table_is_skipped_not_failed():
    """Таблиця порожня ЦІЛКОМ — механізм ще не починав працювати
    (нова функція, свіже розгортання), це не виродження виводу."""
    conn = FakeConn(FakeCursor(
        rows=[(0, 0, 0), (False,)],
        columns=[["rows", "distinct_cluster_count", "nulls_summary"], ["exists"]],
    ))
    result = run_check(conn, _check())

    assert result["status"] == SKIPPED
    assert "не починав працювати" in result["detail"]


def test_table_that_stopped_writing_fails():
    """Рядки колись були, а у вікні немає — джоба перестала писати."""
    conn = FakeConn(FakeCursor(
        rows=[(0, 0, 0), (True,)],
        columns=[["rows", "distinct_cluster_count", "nulls_summary"], ["exists"]],
    ))
    result = run_check(conn, _check())

    assert result["status"] == FAIL
    assert "перестала писати" in result["detail"]


def test_event_driven_check_has_no_time_filter():
    """`window_hours: None` — вік рядків не міряємо, тож WHERE не
    з'являється взагалі."""
    cursor = FakeCursor(rows=[(5, 5, 0)], columns=[["rows", "distinct_cluster_count", "nulls_summary"]])
    run_check(FakeConn(cursor), _check(window_hours=None))

    # Саме вікна немає; `FILTER (WHERE ...)` для підрахунку NULL
    # лишається — тому перевіряємо відсутність інтервалу, не слова WHERE.
    assert "interval" not in cursor.queries[0]
    assert "created_at" not in cursor.queries[0]


@pytest.mark.parametrize("bad", ["drop table x", "a;b", "Mixed", "with-dash", ""])
def test_identifiers_are_whitelisted(bad):
    """Імена таблиць/колонок ідуть у SQL підстановкою (параметризувати
    ідентифікатори не можна), тож друкарська помилка в конфігу не має
    ставати чимось гіршим за помилку."""
    with pytest.raises(ValueError):
        _safe_ident(bad)
