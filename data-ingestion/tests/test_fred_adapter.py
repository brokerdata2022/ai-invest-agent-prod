"""
Тести FredAdapter на збережених прикладах відповіді API — жодних
реальних мережевих викликів (див. .claude/skills/add-data-source, п.3).
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from macro.fred_adapter import FredAdapter, METRICS

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def cpi_response():
    with open(FIXTURES_DIR / "fred_cpi_response.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def adapter():
    return FredAdapter(api_key="fake-key-for-tests", metric_id="cpi")


def test_series_id_resolved_from_metric_id(adapter):
    assert adapter.series_id == "CPIAUCSL"
    assert adapter.source == "fred"


def test_unknown_metric_id_rejected():
    with pytest.raises(ValueError):
        FredAdapter(api_key="fake-key", metric_id="not_a_real_metric")


def test_normalize_produces_expected_records(adapter, cpi_response):
    records = adapter.normalize(cpi_response)

    # Третій запис у фікстурі має value "." (відсутнє значення) — має бути пропущений.
    assert len(records) == 2

    latest = records[0]
    assert latest.source == "fred"
    assert latest.metric_id == "cpi"
    assert latest.value == Decimal("314.121")
    assert latest.observed_at == date(2026, 7, 1)
    assert latest.revision is None  # revision визначає common/db.py, не адаптер
    assert latest.raw_payload["date"] == "2026-07-01"


def test_normalize_skips_missing_values(adapter, cpi_response):
    records = adapter.normalize(cpi_response)
    observed_dates = {r.observed_at for r in records}
    assert date(2026, 5, 1) not in observed_dates  # це саме той запис зі значенням "."


def test_normalize_sets_fetched_at_close_to_now(adapter, cpi_response):
    from datetime import datetime, timezone

    records = adapter.normalize(cpi_response)
    now = datetime.now(timezone.utc)
    for record in records:
        assert (now - record.fetched_at).total_seconds() < 5


def test_all_declared_metrics_have_series_id():
    # Захист від типографічної помилки в METRICS при додаванні нового показника.
    for metric_id, series_id in METRICS.items():
        assert isinstance(metric_id, str) and metric_id
        assert isinstance(series_id, str) and series_id.isupper()
