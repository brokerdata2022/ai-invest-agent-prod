"""
Тести пакетних запитів скринінгу (`analysis/screening/_batch_db.py`) —
межі дати, додані 2026-10-16.

**Навіщо межі.** Запити читали hypertable без обмеження по
`observed_at`, тож планувальник розглядав усі 1211 чанків. Зміряно на
продакшені, бойовий шлях коду, 512 тикерів: **826 мс → 112 мс**; увесь
прогін скринінгу **16.1 с → 6.4 с**.

**Що тут перевіряється.** Не SQL (він потребує БД), а три властивості,
від яких залежить КОРЕКТНІСТЬ: межа задається літеральною датою,
переведення торгових точок у календарні дні має запас, і сам предикат
потрапляє в запит лише тоді, коли межу задали.

БД підміняється курсором (той самий підхід, що test_output_health.py).
"""

from datetime import date, timedelta

import pytest

from screening import _batch_db
from screening._batch_db import (
    _cutoff,
    batch_latest_values,
    batch_series,
    calendar_days_for,
)


class FakeCursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.queries: list = []
        self.params: list = []

    def execute(self, query, params=None):
        self.queries.append(query)
        self.params.append(params)

    def fetchall(self):
        return list(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, rows=()):
        self._cursor = FakeCursor(rows)

    def cursor(self):
        return self._cursor


# ============================================================
# Переведення торгових точок у календарні дні
# ============================================================


def test_calendar_days_add_weekends_and_buffer():
    """63 торгові точки це ~88 календарних днів плюс запас на свята —
    недорахувати тут означає молча обрізати ряд і зіпсувати тренд."""
    assert calendar_days_for(63, 21) == 109


def test_calendar_days_scale_with_points():
    assert calendar_days_for(2, 21) > 21
    assert calendar_days_for(100, 0) > calendar_days_for(50, 0)


# ============================================================
# Межа — ЛІТЕРАЛЬНА дата
# ============================================================


def test_cutoff_is_a_literal_date():
    """РЕГРЕСІЯ: `current_date` у SQL — STABLE-функція, і TimescaleDB не
    відсікає по ній чанки на ПЛАНУВАННІ. Зміряно 2026-10-15 на
    ф'ючерсній історії: саме заміна на літеральну дату дала
    1877 мс → 13.5 мс."""
    cutoff = _cutoff(90)

    assert isinstance(cutoff, date)
    assert cutoff == date.today() - timedelta(days=90)


def test_no_cutoff_without_limit():
    assert _cutoff(None) is None


# ============================================================
# Предикат у запиті
# ============================================================


def test_latest_values_without_bound_uses_current_values_view():
    """Дефолт лишає СТАРУ поведінку — щоб жоден наявний виклик не
    змінив результат непомітно."""
    conn = FakeConn([("aapl_close", 1)])
    batch_latest_values(conn, "twelvedata", ["aapl_close"])

    query = conn.cursor().queries[0]
    assert "v_current_values" in query
    assert "observed_at >=" not in query


def test_latest_values_with_bound_filters_on_hypertable():
    """Фільтр мусить стояти на самій hypertable, а не поверх
    `v_current_values`: та в'юшка сама не має межі по даті, тож
    предикат поверх неї не допоміг би планувальнику."""
    conn = FakeConn([("aapl_close", 1)])
    batch_latest_values(conn, "twelvedata", ["aapl_close"], max_age_days=90)

    query = conn.cursor().queries[0]
    assert "observed_at >= %s" in query
    assert "v_observations_latest_revision" in query
    assert _cutoff(90) in conn.cursor().params[0]


def test_series_bound_applies_to_both_query_shapes():
    for limit in (None, 5):
        conn = FakeConn([("aapl_close", date(2026, 10, 1), 1)])
        batch_series(conn, "twelvedata", ["aapl_close"], limit_per_metric=limit, max_age_days=120)

        query = conn.cursor().queries[0]
        assert "observed_at >= %s" in query, f"limit_per_metric={limit}"


def test_series_without_bound_has_no_date_predicate():
    conn = FakeConn([("aapl_revenue", date(2011, 6, 30), 1)])
    batch_series(conn, "sec_edgar", ["aapl_revenue"])

    assert "observed_at >=" not in conn.cursor().queries[0]


def test_empty_metric_ids_make_no_query():
    conn = FakeConn()

    assert batch_latest_values(conn, "twelvedata", [], max_age_days=90) == {}
    assert batch_series(conn, "twelvedata", [], max_age_days=90) == {}
    assert conn.cursor().queries == []


# ============================================================
# Відсіяне межею — ГОЛОСНО, але один раз на джерело
# ============================================================


def test_age_exclusions_reported_once_per_source(monkeypatch, caplog):
    """Сам звіт робить запит БЕЗ межі дати — тобто той самий дорогий
    шлях. Живий замір 2026-10-16: 6 викликів `tier_b` на `sec_edgar`
    дали 6 звітів і повернули прогін із 4.6 с назад до 9.1 с."""
    monkeypatch.setattr(_batch_db, "_age_report_done", set())
    calls = {"n": 0}

    def fake_report(conn, source, metric_ids, result, cutoff):
        if source in _batch_db._age_report_done:
            return
        _batch_db._age_report_done.add(source)
        calls["n"] += 1

    monkeypatch.setattr(_batch_db, "_report_excluded_by_age", fake_report)

    for _ in range(6):
        conn = FakeConn()
        batch_series(conn, "sec_edgar", ["hal_revenue"], max_age_days=1100)

    assert calls["n"] == 1


def test_no_report_when_nothing_missing(monkeypatch):
    """Усе знайшлось — скаржитись нема на що."""
    monkeypatch.setattr(_batch_db, "_age_report_done", set())
    conn = FakeConn([("hal_revenue", date(2026, 6, 30), 1)])

    batch_series(conn, "sec_edgar", ["hal_revenue"], max_age_days=1100)

    # Звіт шукає ВІДСУТНІ ряди; тут їх немає, тож другого запиту не було.
    assert len(conn.cursor().queries) == 1
