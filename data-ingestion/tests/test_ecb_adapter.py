"""
Тести EcbAdapter на збереженому прикладі відповіді API (CSV,
format=csvdata) — жодних реальних мережевих викликів.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from macro.ecb_adapter import EcbAdapter, METRICS

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def hicp_response():
    with open(FIXTURES_DIR / "ecb_hicp_response.csv", encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def adapter():
    return EcbAdapter(metric_id="eurozone_hicp")


def test_flow_and_series_key_resolved_from_metric_id(adapter):
    assert adapter.flow_ref == "HICP"
    assert adapter.series_key == "M.U2.N.000000.4D0.ANR"
    assert adapter.source == "ecb"


def test_unknown_metric_id_rejected():
    with pytest.raises(ValueError):
        EcbAdapter(metric_id="not_a_real_metric")


def test_normalize_produces_expected_records(adapter, hicp_response):
    records = adapter.normalize(hicp_response)

    # Третій рядок фікстури має порожнє OBS_VALUE — має бути пропущений.
    assert len(records) == 2

    latest = records[-1]
    assert latest.source == "ecb"
    assert latest.metric_id == "eurozone_hicp"
    assert latest.value == Decimal("2.1")
    assert latest.observed_at == date(2026, 6, 1)
    assert latest.revision is None  # revision визначає common/db.py, не адаптер
    assert latest.raw_payload["TIME_PERIOD"] == "2026-06"


def test_normalize_skips_missing_values(adapter, hicp_response):
    records = adapter.normalize(hicp_response)
    observed_dates = {r.observed_at for r in records}
    assert date(2026, 7, 1) not in observed_dates  # це саме той рядок з порожнім OBS_VALUE


def test_normalize_sets_fetched_at_close_to_now(adapter, hicp_response):
    from datetime import datetime, timezone

    records = adapter.normalize(hicp_response)
    now = datetime.now(timezone.utc)
    for record in records:
        assert (now - record.fetched_at).total_seconds() < 5


def test_daily_time_period_parsed():
    # Deposit Facility Rate — щоденний ряд, TIME_PERIOD у форматі YYYY-MM-DD.
    adapter = EcbAdapter(metric_id="eurozone_deposit_rate")
    csv_text = (
        "KEY,FREQ,REF_AREA,TIME_PERIOD,OBS_VALUE\n"
        "FM.D.U2.EUR.4F.KR.DFR.LEV,D,U2,2026-08-24,2.25\n"
    )
    records = adapter.normalize(csv_text)
    assert len(records) == 1
    assert records[0].observed_at == date(2026, 8, 24)
    assert records[0].value == Decimal("2.25")


def test_all_declared_metrics_have_flow_and_series_key():
    # Захист від типографічної помилки в METRICS при додаванні нового показника.
    for metric_id, (flow_ref, series_key) in METRICS.items():
        assert isinstance(metric_id, str) and metric_id
        assert isinstance(flow_ref, str) and flow_ref.isupper()
        assert isinstance(series_key, str) and series_key
