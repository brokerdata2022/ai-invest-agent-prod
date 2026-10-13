"""
Тести economic_calendar на збереженому прикладі відповіді (JSON,
реальна відповідь `GET ff_calendar_thisweek.json`, отримана живим
запитом 2026-09-26) — жодних реальних мережевих викликів.
"""

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from economic_calendar import find_event, find_upcoming_event

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def calendar_events():
    with open(FIXTURES_DIR / "ff_calendar_thisweek_response.json", encoding="utf-8") as f:
        return json.load(f)


def test_finds_confirmed_unemployment_claims_event(calendar_events):
    # Live-підтверджено 2026-09-26: реальний рядок у фікстурі.
    event = find_event("initial_jobless_claims", calendar_events, on_date=date(2026, 9, 24))
    assert event is not None
    assert event["title"] == "Unemployment Claims"
    assert event["forecast"] == "201K"
    assert datetime.fromisoformat(event["date"]).time().isoformat() == "08:30:00"


def test_returns_none_when_metric_not_scheduled_this_week(calendar_events):
    # cpi/nonfarm_payrolls тощо не заплановані цього конкретного тижня.
    assert find_event("cpi", calendar_events, on_date=date(2026, 9, 24)) is None
    assert find_event("nonfarm_payrolls", calendar_events, on_date=date(2026, 9, 24)) is None


def test_returns_none_for_unknown_metric_id(calendar_events):
    assert find_event("doge", calendar_events, on_date=date(2026, 9, 24)) is None


def test_returns_none_when_date_does_not_match(calendar_events):
    # Unemployment Claims є, але не на цю дату.
    assert find_event("initial_jobless_claims", calendar_events, on_date=date(2026, 1, 1)) is None


def test_excludes_core_variant_for_headline_cpi():
    fake_events = [
        {"title": "Core CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00", "impact": "High", "forecast": "0.3%", "previous": "0.3%"},
    ]
    # "Core CPI m/m" не повинен зловитись патерном "cpi m/m" для headline "cpi".
    assert find_event("cpi", fake_events, on_date=date(2026, 10, 14)) is None
    # Але для "core_cpi" — має знайтись.
    event = find_event("core_cpi", fake_events, on_date=date(2026, 10, 14))
    assert event is not None
    assert event["title"] == "Core CPI m/m"


def test_handles_malformed_event_date_gracefully():
    fake_events = [
        {"title": "CPI m/m", "country": "USD", "date": "not-a-date", "impact": "High", "forecast": "", "previous": ""},
    ]
    assert find_event("cpi", fake_events, on_date=date(2026, 10, 14)) is None


def test_returns_none_for_non_list_events():
    assert find_event("cpi", {"unexpected": "shape"}, on_date=date(2026, 9, 24)) is None


def test_country_filter_prevents_cross_country_false_match():
    # Regression: "Unemployment Rate" — та сама назва для США й
    # єврозони. Без фільтра по country US-метрика могла б зловити
    # EUR-подію (чи навпаки) з тим самим заголовком.
    fake_events = [
        {"title": "Unemployment Rate", "country": "EUR", "date": "2026-10-14T05:00:00-04:00", "impact": "Medium", "forecast": "6.3%", "previous": "6.3%"},
    ]
    assert find_event("unemployment_rate", fake_events, on_date=date(2026, 10, 14)) is None
    event = find_event("eurozone_unemployment_rate", fake_events, on_date=date(2026, 10, 14))
    assert event is not None
    assert event["country"] == "EUR"


def test_find_upcoming_event_for_eurozone_hicp_excludes_core():
    fake_events = [
        {"title": "Core CPI Flash Estimate y/y", "country": "EUR", "date": "2026-10-01T05:00:00-04:00", "impact": "Medium", "forecast": "2.3%", "previous": "2.3%"},
        {"title": "CPI Flash Estimate y/y", "country": "EUR", "date": "2026-10-01T05:00:00-04:00", "impact": "High", "forecast": "2.1%", "previous": "2.0%"},
    ]
    event = find_upcoming_event("eurozone_hicp", fake_events, today=date(2026, 9, 26))
    assert event is not None
    assert event["title"] == "CPI Flash Estimate y/y"


def test_find_upcoming_event_for_japan_cpi():
    fake_events = [
        {"title": "BOJ Core CPI y/y", "country": "JPY", "date": "2026-09-25T01:00:00-04:00", "impact": "Low", "forecast": "1.5%", "previous": "1.5%"},
        {"title": "National CPI y/y", "country": "JPY", "date": "2026-09-30T19:30:00-04:00", "impact": "High", "forecast": "2.9%", "previous": "3.1%"},
    ]
    # "BOJ Core CPI y/y" — ІНШИЙ показник (BOJ-власний, не наш
    # japan_cpi з e-Stat) — не повинен зловитись.
    event = find_upcoming_event("japan_cpi", fake_events, today=date(2026, 9, 26))
    assert event is not None
    assert event["title"] == "National CPI y/y"


def test_find_upcoming_event_returns_none_outside_horizon():
    fake_events = [
        {"title": "Deposit Facility Rate", "country": "EUR", "date": "2026-11-15T07:45:00-04:00", "impact": "High", "forecast": "", "previous": "2.00%"},
    ]
    assert find_upcoming_event("eurozone_deposit_rate", fake_events, today=date(2026, 9, 26), horizon_days=7) is None


def test_find_upcoming_event_returns_earliest_match():
    fake_events = [
        {"title": "Monetary Policy Statement", "country": "JPY", "date": "2026-10-02T00:00:00-04:00", "impact": "High", "forecast": "", "previous": ""},
        {"title": "BOJ Policy Rate", "country": "JPY", "date": "2026-09-30T00:00:00-04:00", "impact": "High", "forecast": "0.75%", "previous": "0.75%"},
    ]
    event = find_upcoming_event("japan_policy_rate", fake_events, today=date(2026, 9, 26))
    assert event is not None
    assert event["title"] == "BOJ Policy Rate"


def test_find_upcoming_event_returns_none_for_non_list_events():
    assert find_upcoming_event("eurozone_hicp", {"unexpected": "shape"}, today=date(2026, 9, 26)) is None
