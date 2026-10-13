"""
Тести для чистої логіки Tier A (analysis/screening/tier_a.py) —
тільки passes_tier_a(), без звернень до БД. Сам запит до БД (SQL у
_tickers_with_quotes/_latest_value/_avg_dollar_volume) не тестується
тут — потребує реальної TimescaleDB, перший живий прогін користувача
це і перевірить (той самий підхід, що й для кожного адаптера).
"""

from datetime import date
from decimal import Decimal

import pytest

from screening.tier_a import (
    passes_tier_a,
    avg_dollar_volume_from_series,
    MIN_PRICE,
    MIN_MARKET_CAP,
    MIN_AVG_DOLLAR_VOLUME,
)


def test_passes_all_thresholds():
    assert passes_tier_a(
        price=Decimal("50"),
        market_cap=Decimal("50000000000"),
        avg_dollar_volume=Decimal("20000000"),
    ) is True


def test_fails_on_price_at_or_below_threshold():
    assert passes_tier_a(MIN_PRICE, Decimal("50000000000"), Decimal("20000000")) is False
    assert passes_tier_a(Decimal("5"), Decimal("50000000000"), Decimal("20000000")) is False


def test_fails_on_market_cap_at_or_below_threshold():
    assert passes_tier_a(Decimal("50"), MIN_MARKET_CAP, Decimal("20000000")) is False
    assert passes_tier_a(Decimal("50"), Decimal("1000000000"), Decimal("20000000")) is False


def test_fails_on_volume_at_or_below_threshold():
    assert passes_tier_a(Decimal("50"), Decimal("50000000000"), MIN_AVG_DOLLAR_VOLUME) is False
    assert passes_tier_a(Decimal("50"), Decimal("50000000000"), Decimal("1000000")) is False


def test_fails_when_volume_is_none():
    assert passes_tier_a(Decimal("50"), Decimal("50000000000"), None) is False


def test_passes_just_above_all_thresholds():
    assert passes_tier_a(
        price=MIN_PRICE + Decimal("0.01"),
        market_cap=MIN_MARKET_CAP + Decimal("1"),
        avg_dollar_volume=MIN_AVG_DOLLAR_VOLUME + Decimal("1"),
    ) is True


# --- avg_dollar_volume_from_series ---
# Заміняє SQL JOIN...AVG(), що був у попередній (по-тикерній запит)
# версії _avg_dollar_volume() до фіксу N+1 (docs/decisions.md, 2026-09-25):
# ту саму арифметику тепер рахує ця чиста функція над серіями, уже
# вибраними одним batched-запитом на всі тикери (screening._batch_db).

def test_avg_dollar_volume_from_series_matching_dates():
    closes = [(date(2026, 1, 3), Decimal("10")), (date(2026, 1, 2), Decimal("20"))]
    volumes = [(date(2026, 1, 3), Decimal("100")), (date(2026, 1, 2), Decimal("50"))]
    # (10*100 + 20*50) / 2 = (1000 + 1000) / 2 = 1000
    assert avg_dollar_volume_from_series(closes, volumes) == Decimal("1000")


def test_avg_dollar_volume_from_series_ignores_unmatched_dates():
    closes = [(date(2026, 1, 3), Decimal("10")), (date(2026, 1, 2), Decimal("20"))]
    volumes = [(date(2026, 1, 3), Decimal("100"))]  # 2026-01-02 відсутній у volumes
    # тільки спільна дата 2026-01-03 враховується: 10*100 / 1 = 1000
    assert avg_dollar_volume_from_series(closes, volumes) == Decimal("1000")


def test_avg_dollar_volume_from_series_none_when_no_overlap():
    closes = [(date(2026, 1, 3), Decimal("10"))]
    volumes = [(date(2026, 1, 2), Decimal("100"))]
    assert avg_dollar_volume_from_series(closes, volumes) is None


def test_avg_dollar_volume_from_series_none_on_empty_input():
    assert avg_dollar_volume_from_series([], []) is None
