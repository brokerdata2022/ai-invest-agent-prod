"""
Тести long_screen.py — чиста функція рішення, усі гілки критеріїв.
"""

from decimal import Decimal

from crypto_screening.long_screen import screen_long

GOOD_ARGS = dict(
    symbol="BTCUSDT",
    trend_confirmed=True,
    oi_change_pct=5.0,
    rsi_value=60.0,
    funding_rate=Decimal("0.0001"),
)


def test_qualifies_when_all_criteria_met():
    signal = screen_long(**GOOD_ARGS)

    assert signal.qualifies is True
    assert signal.direction == "up"
    assert signal.reasons == []


def test_fails_when_trend_not_confirmed():
    signal = screen_long(**{**GOOD_ARGS, "trend_confirmed": False})

    assert signal.qualifies is False
    assert signal.direction == "none"
    assert any("тренд" in r for r in signal.reasons)


def test_fails_when_oi_not_rising():
    signal = screen_long(**{**GOOD_ARGS, "oi_change_pct": -2.0})

    assert signal.qualifies is False
    assert any("Open Interest" in r for r in signal.reasons)


def test_fails_when_oi_change_missing():
    signal = screen_long(**{**GOOD_ARGS, "oi_change_pct": None})

    assert signal.qualifies is False


def test_fails_when_rsi_too_low():
    signal = screen_long(**{**GOOD_ARGS, "rsi_value": 30.0})

    assert signal.qualifies is False
    assert any("RSI" in r for r in signal.reasons)


def test_fails_when_rsi_too_high():
    signal = screen_long(**{**GOOD_ARGS, "rsi_value": 85.0})

    assert signal.qualifies is False


def test_fails_when_funding_rate_overheated():
    signal = screen_long(**{**GOOD_ARGS, "funding_rate": Decimal("0.001")})

    assert signal.qualifies is False
    assert any("funding" in r for r in signal.reasons)


def test_funding_rate_none_does_not_block():
    signal = screen_long(**{**GOOD_ARGS, "funding_rate": None})

    assert signal.qualifies is True
