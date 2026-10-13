"""
Тести чистої логіки Tier B (analysis/screening/tier_b.py) — без БД.
SQL (_series) не тестується тут, живий прогін користувача перевірить,
як і для кожного попереднього кроку.
"""

from datetime import date
from decimal import Decimal

import pytest

from screening.tier_b import (
    all_positive_last_n,
    yoy_growth,
    passes_tier_b,
    MIN_REVENUE_YOY,
    MIN_EPS_YOY,
    MAX_SHARES_YOY,
    MAX_LIABILITIES_TO_ASSETS,
)


def _series(pairs):
    """[(рядок-дата, число), ...] -> [(date, Decimal), ...] за спаданням дати."""
    return sorted(
        [(date.fromisoformat(d), Decimal(str(v))) for d, v in pairs],
        key=lambda x: x[0], reverse=True,
    )


# --- all_positive_last_n ---

def test_all_positive_last_n_true_when_all_positive():
    series = _series([("2026-06-30", 10), ("2026-03-31", 5), ("2025-12-31", 1), ("2025-09-30", 3)])
    assert all_positive_last_n(series) is True


def test_all_positive_last_n_false_when_one_negative():
    series = _series([("2026-06-30", 10), ("2026-03-31", -5), ("2025-12-31", 1), ("2025-09-30", 3)])
    assert all_positive_last_n(series) is False


def test_all_positive_last_n_false_when_not_enough_history():
    series = _series([("2026-06-30", 10), ("2026-03-31", 5)])  # тільки 2 квартали
    assert all_positive_last_n(series) is False


# --- yoy_growth ---

def test_yoy_growth_finds_pair_within_tolerance():
    series = _series([("2026-06-30", 110), ("2025-06-27", 100)])  # 368 днів різниця, в межах допуску
    growth = yoy_growth(series)
    assert growth == Decimal("110") / Decimal("100") - 1


def test_yoy_growth_none_when_no_year_ago_entry():
    series = _series([("2026-06-30", 110), ("2026-03-31", 105)])  # обидва занадто близько
    assert yoy_growth(series) is None


def test_yoy_growth_none_on_empty_series():
    assert yoy_growth([]) is None


def test_yoy_growth_none_when_year_ago_value_is_zero():
    series = _series([("2026-06-30", 110), ("2025-06-30", 0)])
    assert yoy_growth(series) is None


def test_yoy_growth_negative_when_declined():
    series = _series([("2026-06-30", 90), ("2025-06-30", 100)])
    assert yoy_growth(series) == Decimal("-0.1")


# --- passes_tier_b (інтеграція чотирьох критеріїв) ---

def _good_net_income():
    return _series([("2026-06-30", 10), ("2026-03-31", 8), ("2025-12-31", 9), ("2025-09-30", 7)])


def _good_revenue():
    # +15% YoY — вище порогу 10%
    return _series([("2026-06-30", 115), ("2025-06-30", 100)])


def _good_eps():
    # +20% YoY — вище порогу 15%
    return _series([("2026-06-30", "1.20"), ("2025-06-30", "1.00")])


def _good_shares():
    # +1% YoY — нижче порогу толерантності 3%
    return _series([("2026-06-30", 1010000000), ("2025-06-30", 1000000000)])


def test_passes_tier_b_all_criteria_met():
    ok, details = passes_tier_b(
        _good_net_income(), _good_revenue(), _good_eps(), _good_shares(),
        liabilities=Decimal("400"), assets=Decimal("1000"),  # L/A = 0.4 < 0.6
    )
    assert ok is True
    assert details["revenue_yoy"] > MIN_REVENUE_YOY
    assert details["eps_yoy"] > MIN_EPS_YOY
    assert details["shares_yoy"] < MAX_SHARES_YOY
    assert details["liabilities_to_assets"] < MAX_LIABILITIES_TO_ASSETS


def test_fails_when_not_profitable():
    unprofitable = _series([("2026-06-30", -1), ("2026-03-31", 8), ("2025-12-31", 9), ("2025-09-30", 7)])
    ok, _ = passes_tier_b(
        unprofitable, _good_revenue(), _good_eps(), _good_shares(),
        liabilities=Decimal("400"), assets=Decimal("1000"),
    )
    assert ok is False


def test_fails_when_revenue_growth_too_low():
    flat_revenue = _series([("2026-06-30", 101), ("2025-06-30", 100)])  # +1%, поріг 10%
    ok, details = passes_tier_b(
        _good_net_income(), flat_revenue, _good_eps(), _good_shares(),
        liabilities=Decimal("400"), assets=Decimal("1000"),
    )
    assert ok is False
    assert "eps_yoy" not in details  # спинилось на revenue, далі не рахувало


def test_fails_when_shares_diluted_too_much():
    diluted = _series([("2026-06-30", 1100000000), ("2025-06-30", 1000000000)])  # +10%, поріг 3%
    ok, _ = passes_tier_b(
        _good_net_income(), _good_revenue(), _good_eps(), diluted,
        liabilities=Decimal("400"), assets=Decimal("1000"),
    )
    assert ok is False


def test_fails_when_liabilities_to_assets_too_high():
    ok, _ = passes_tier_b(
        _good_net_income(), _good_revenue(), _good_eps(), _good_shares(),
        liabilities=Decimal("700"), assets=Decimal("1000"),  # L/A = 0.7 > 0.6
    )
    assert ok is False


def test_fails_when_liabilities_or_assets_missing():
    ok, _ = passes_tier_b(
        _good_net_income(), _good_revenue(), _good_eps(), _good_shares(),
        liabilities=None, assets=Decimal("1000"),
    )
    assert ok is False
