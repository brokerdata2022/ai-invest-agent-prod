"""
Тести compare_one()/run_comparison_cycle() з підміненими БД-функціями
(fetch_recent/get_detected_entries/mark_processed/save_comparison) —
той самий підхід, що test_relevance_filter.py:test_analyze_article_calls_deepseek_and_parses
(monkeypatch, без реальної БД).
"""

from expectations.compare_releases import compare_one, run_comparison_cycle

CPI_ENTRY = {
    "id": 1,
    "source": "fred",
    "metric_id": "cpi",
    "expected_value": "0.4%",
}


def test_compare_one_computes_surprise(monkeypatch):
    monkeypatch.setattr(
        "expectations.compare_releases.fetch_recent",
        lambda conn, source, metric_id, limit: [
            {"value": 315.75, "observed_at": "2026-09-30"},
            {"value": 314.5, "observed_at": "2026-08-31"},
        ],
    )

    result = compare_one(conn=None, entry=CPI_ENTRY)

    assert result is not None
    assert result.metric_id == "cpi"
    assert result.expected_value_parsed == 0.4
    assert result.actual_value > 0.39  # ~0.397%
    assert result.surprise < 0  # факт трохи нижчий за прогноз
    assert result.comparison_method == "mom_pct"


def test_compare_one_returns_none_when_no_forecast():
    entry = dict(CPI_ENTRY, expected_value=None, metric_id="mortgage_rate_30y")
    result = compare_one(conn=None, entry=entry)
    assert result is None


def test_compare_one_returns_none_when_metric_unregistered():
    entry = dict(CPI_ENTRY, metric_id="unknown_metric")
    result = compare_one(conn=None, entry=entry)
    assert result is None


def test_compare_one_returns_none_when_not_enough_history(monkeypatch):
    monkeypatch.setattr(
        "expectations.compare_releases.fetch_recent",
        lambda conn, source, metric_id, limit: [{"value": 315.75, "observed_at": "2026-09-30"}],
    )
    result = compare_one(conn=None, entry=CPI_ENTRY)
    assert result is None


def test_run_comparison_cycle_saves_and_marks_processed(monkeypatch):
    saved = []
    processed_ids = []

    monkeypatch.setattr(
        "expectations.compare_releases.get_detected_entries",
        lambda conn: [CPI_ENTRY],
    )
    monkeypatch.setattr(
        "expectations.compare_releases.fetch_recent",
        lambda conn, source, metric_id, limit: [
            {"value": 315.75, "observed_at": "2026-09-30"},
            {"value": 314.5, "observed_at": "2026-08-31"},
        ],
    )
    monkeypatch.setattr(
        "expectations.compare_releases.save_comparison",
        lambda conn, result: saved.append(result),
    )
    monkeypatch.setattr(
        "expectations.compare_releases.mark_processed",
        lambda conn, log_id: processed_ids.append(log_id),
    )

    results = run_comparison_cycle(conn=None)

    assert len(results) == 1
    assert len(saved) == 1
    assert processed_ids == [1]


def test_run_comparison_cycle_marks_processed_even_when_no_forecast(monkeypatch):
    entry = dict(CPI_ENTRY, expected_value=None, metric_id="mortgage_rate_30y")
    processed_ids = []

    monkeypatch.setattr("expectations.compare_releases.get_detected_entries", lambda conn: [entry])
    monkeypatch.setattr(
        "expectations.compare_releases.mark_processed",
        lambda conn, log_id: processed_ids.append(log_id),
    )

    results = run_comparison_cycle(conn=None)

    assert results == []
    assert processed_ids == [1]  # реліз усе одно вважається обробленим


def test_run_comparison_cycle_respects_metric_filter(monkeypatch):
    other_entry = dict(CPI_ENTRY, id=2, metric_id="unemployment_rate", expected_value="4.1%")
    processed_ids = []

    monkeypatch.setattr(
        "expectations.compare_releases.get_detected_entries",
        lambda conn: [CPI_ENTRY, other_entry],
    )
    monkeypatch.setattr(
        "expectations.compare_releases.fetch_recent",
        lambda conn, source, metric_id, limit: [{"value": 4.1, "observed_at": "2026-09-30"}],
    )
    monkeypatch.setattr("expectations.compare_releases.save_comparison", lambda conn, result: None)
    monkeypatch.setattr(
        "expectations.compare_releases.mark_processed",
        lambda conn, log_id: processed_ids.append(log_id),
    )

    results = run_comparison_cycle(conn=None, metric_filter="unemployment_rate")

    assert len(results) == 1
    assert results[0].metric_id == "unemployment_rate"
    assert processed_ids == [2]
