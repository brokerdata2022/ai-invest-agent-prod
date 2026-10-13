"""
Тести BojAdapter на прикладі відповіді (структура підтверджена живим
прогоном 2026-08-30 — 5 записів japan_policy_rate успішно збережено).
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from macro.boj_adapter import BojAdapter, METRICS

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def policy_rate_response():
    with open(FIXTURES_DIR / "boj_policy_rate_response.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def adapter():
    return BojAdapter(metric_id="japan_policy_rate")


def test_db_and_series_code_resolved_from_metric_id(adapter):
    assert adapter.db_name == "FM01"
    assert adapter.series_code == "STRDCLUCON"
    assert adapter.source == "boj"


def test_unknown_metric_id_rejected():
    with pytest.raises(ValueError):
        BojAdapter(metric_id="not_a_real_metric")


def test_normalize_produces_expected_records(adapter, policy_rate_response):
    records = adapter.normalize(policy_rate_response)
    assert len(records) == 3
    latest = records[-1]
    assert latest.source == "boj"
    assert latest.metric_id == "japan_policy_rate"
    assert latest.value == Decimal("0.510")
    assert latest.observed_at == date(2026, 8, 29)
    assert latest.revision is None


def test_normalize_skips_null_values(adapter, policy_rate_response):
    records = adapter.normalize(policy_rate_response)
    observed_dates = {r.observed_at for r in records}
    assert date(2026, 8, 28) not in observed_dates


def test_normalize_applies_limit(adapter, policy_rate_response):
    records = adapter.normalize(policy_rate_response, limit=2)
    assert len(records) == 2
    assert records[-1].observed_at == date(2026, 8, 29)


def test_normalize_sets_fetched_at_close_to_now(adapter, policy_rate_response):
    from datetime import datetime, timezone
    records = adapter.normalize(policy_rate_response)
    now = datetime.now(timezone.utc)
    for record in records:
        assert (now - record.fetched_at).total_seconds() < 5


def test_normalize_returns_empty_on_unexpected_structure(adapter):
    records = adapter.normalize({"GET_STATS": {"RESULT": {"STATUS": 200}}})
    assert records == []


def test_all_declared_metrics_have_db_and_series_code():
    for metric_id, (db_name, series_code) in METRICS.items():
        assert isinstance(metric_id, str) and metric_id
        assert isinstance(db_name, str) and db_name.isupper()
        assert isinstance(series_code, str) and series_code
