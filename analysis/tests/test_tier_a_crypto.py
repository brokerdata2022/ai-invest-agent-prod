"""
Тести crypto_screening/tier_a.py — чиста функція, синтетичні агреговані
дані (той самий стиль, що test_aggregate_sources.py). Назва файлу з
суфіксом _crypto, щоб не колізити з analysis/tests/ тестами Tier A
акцій (analysis/screening/tier_a.py).
"""

from datetime import date
from decimal import Decimal

from crypto_screening.tier_a import check_tier_a

TODAY = date(2026, 9, 27)

GOOD_AGGREGATED = {
    "quote_volume_24h": Decimal("50000000"),
    "open_interest_usd": Decimal("10000000"),
    "exchange_count": 3,
}
OLD_ONBOARD = date(2020, 1, 1)


def test_eligible_when_all_criteria_met():
    result = check_tier_a("BTCUSDT", GOOD_AGGREGATED, OLD_ONBOARD, TODAY)

    assert result.eligible is True
    assert result.reasons == []


def test_rejects_low_volume():
    aggregated = {**GOOD_AGGREGATED, "quote_volume_24h": Decimal("1000")}
    result = check_tier_a("LOWVOLUSDT", aggregated, OLD_ONBOARD, TODAY)

    assert result.eligible is False
    assert any("обсяг" in r for r in result.reasons)


def test_rejects_low_open_interest():
    aggregated = {**GOOD_AGGREGATED, "open_interest_usd": Decimal("100")}
    result = check_tier_a("LOWOIUSDT", aggregated, OLD_ONBOARD, TODAY)

    assert result.eligible is False
    assert any("OI" in r for r in result.reasons)


def test_single_exchange_listing_is_eligible():
    # 2026-10-03, живий кейс AINUSDT: MIN_EXCHANGE_COUNT знижено до 1 —
    # ліквідна монета ЛИШЕ на одній біржі більше не відсіюється через
    # це саме по собі (обсяг/OI лишаються справжнім гейтом).
    aggregated = {**GOOD_AGGREGATED, "exchange_count": 1}
    result = check_tier_a("SOLOUSDT", aggregated, OLD_ONBOARD, TODAY)

    assert result.eligible is True
    assert result.reasons == []


def test_rejects_zero_exchange_listing():
    # Теоретичний крайній випадок (на практиці не трапляється —
    # aggregate_snapshots() ніколи не заведе запис із 0 бірж) — сам
    # критерій і далі коректно відхиляє, якщо exchange_count=0.
    aggregated = {**GOOD_AGGREGATED, "exchange_count": 0}
    result = check_tier_a("GHOSTUSDT", aggregated, OLD_ONBOARD, TODAY)

    assert result.eligible is False
    assert any("бірж" in r for r in result.reasons)


def test_rejects_recently_listed():
    result = check_tier_a("NEWUSDT", GOOD_AGGREGATED, date(2026, 9, 20), TODAY)

    assert result.eligible is False
    assert any("лістингу" in r for r in result.reasons)


def test_rejects_unknown_onboard_date():
    result = check_tier_a("UNKNOWNUSDT", GOOD_AGGREGATED, None, TODAY)

    assert result.eligible is False
    assert any("невідома дата" in r for r in result.reasons)


def test_accumulates_multiple_rejection_reasons():
    aggregated = {"quote_volume_24h": Decimal("1"), "open_interest_usd": Decimal("1"), "exchange_count": 0}
    result = check_tier_a("BADUSDT", aggregated, None, TODAY)

    assert result.eligible is False
    assert len(result.reasons) == 4
