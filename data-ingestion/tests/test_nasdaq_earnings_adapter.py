"""
Тести адаптера календаря звітності (companies/nasdaq_earnings_adapter.py).

Найважливіший тест тут — дужковий мінус у прогнозі EPS: "($0.15)" це
збиток, і прочитаний як прибуток він перевернув би знак сюрпризу.
Решта — про те, що неофіційне джерело не валить збір цілком.
"""

from datetime import date
from decimal import Decimal

import pytest

from companies.nasdaq_earnings_adapter import SOURCE, _parse_money, collect, normalize

ROWS = [
    {
        "symbol": "GE",
        "name": "GE Aerospace",
        "time": "time-pre-market",
        "epsForecast": "$2.01",
        "fiscalQuarterEnding": "Sep/2026",
    },
    {
        "symbol": "NFLX",
        "name": "Netflix, Inc.",
        "time": "time-after-hours",
        "epsForecast": "$0.82",
        "fiscalQuarterEnding": "Sep/2026",
    },
]


# ============================================================
# Розбір прогнозу EPS
# ============================================================


def test_parses_dollar_amount():
    assert _parse_money("$2.01") == Decimal("2.01")


def test_parentheses_mean_loss_not_profit():
    """Бухгалтерський мінус. Знак тут важливіший за значення: прогноз
    збитку, прочитаний як прибуток, перевернув би сюрприз."""
    assert _parse_money("($0.15)") == Decimal("-0.15")


def test_explicit_minus_is_kept():
    assert _parse_money("-$0.15") == Decimal("-0.15")


@pytest.mark.parametrize("raw", [None, "", "N/A", "-", "$"])
def test_missing_forecast_is_none_not_zero(raw):
    """Нуль означав би "прогноз нульового прибутку" — це інше
    твердження, ніж "прогнозу немає"."""
    assert _parse_money(raw) is None


# ============================================================
# Нормалізація
# ============================================================


def test_normalize_maps_fields_and_date():
    records = normalize(ROWS, date(2026, 10, 20))

    assert [r["ticker"] for r in records] == ["GE", "NFLX"]
    assert all(r["report_date"] == date(2026, 10, 20) for r in records)
    assert all(r["source"] == SOURCE for r in records)
    assert records[0]["report_time"] == "до відкриття"
    assert records[1]["report_time"] == "після закриття"
    assert records[0]["eps_forecast"] == Decimal("2.01")


def test_normalize_skips_rows_without_symbol():
    records = normalize([{"name": "Без тикера"}, *ROWS], date(2026, 10, 20))

    assert len(records) == 2


def test_normalize_uppercases_ticker():
    records = normalize([{"symbol": " nflx "}], date(2026, 10, 20))

    assert records[0]["ticker"] == "NFLX"


def test_unknown_time_label_passes_through():
    """Джерело неофіційне — нова мітка часу не має ставати None."""
    records = normalize([{"symbol": "X", "time": "time-something-new"}], date(2026, 10, 20))

    assert records[0]["report_time"] == "time-something-new"


# ============================================================
# Збір по днях
# ============================================================


def test_collect_skips_weekends(monkeypatch):
    asked: list = []

    def fake_fetch_day(session, day):
        asked.append(day)
        return []

    monkeypatch.setattr(
        "companies.nasdaq_earnings_adapter.fetch_day", fake_fetch_day
    )
    # 2026-10-15 — четвер; горизонт 4 дні захоплює сб/нд.
    collect(horizon_days=4, start=date(2026, 10, 15))

    assert [d.weekday() for d in asked] == [3, 4, 0]  # чт, пт, пн


def test_collect_survives_failing_day(monkeypatch):
    """Збій одного дня не має забирати решту горизонту."""
    def fake_fetch_day(session, day):
        if day == date(2026, 10, 19):
            raise RuntimeError("502")
        return ROWS

    monkeypatch.setattr("companies.nasdaq_earnings_adapter.fetch_day", fake_fetch_day)
    # 19.10 — понеділок (падає), 20.10 — вівторок (віддає дані).
    records = collect(horizon_days=1, start=date(2026, 10, 19))

    assert len(records) == 2  # лише вівторок
    assert all(r["report_date"] == date(2026, 10, 20) for r in records)
