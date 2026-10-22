"""
Тести журналу наслідків (monitoring/outcomes.py + _outcomes_db).

Головне, що тут перевіряється — не арифметика відсотка, а те, ЩО саме
входить у знаменник. Без смуги `flat` і без відсіву `no_claim` hit rate
показував би ~50% на випадковому шумі й виглядав як метрика
(docs/decisions.md 2026-10-15).
"""

import pytest

from _outcomes_db import (
    VENUE_BINANCE_FUTURES,
    VENUE_DB_STOCK,
    VENUE_DB_WATCHLIST,
    _venue_for_trading_list,
)
from outcomes import (
    CORRECT,
    FLAT,
    INCORRECT,
    NO_CLAIM,
    UNAVAILABLE,
    hit_rate,
    is_directional,
    realized_pct,
    verdict_for,
)

BAND = 2.0


# ============================================================
# Напрямок як ТВЕРДЖЕННЯ
# ============================================================


@pytest.mark.parametrize("direction", ["up", "down", "long", "short", "UP", "Short"])
def test_directional_claims(direction):
    assert is_directional(direction) is True


@pytest.mark.parametrize("direction", ["watch", "unclear", "neutral", "candidate", "", None])
def test_non_directional_claims(direction):
    """`watch`/`unclear` — запрошення подивитись, не прогноз руху."""
    assert is_directional(direction) is False


# ============================================================
# Вердикт
# ============================================================


def test_up_call_on_rise_is_correct():
    assert verdict_for("up", 5.0, BAND) == CORRECT


def test_up_call_on_fall_is_incorrect():
    assert verdict_for("up", -5.0, BAND) == INCORRECT


def test_down_call_on_fall_is_correct():
    assert verdict_for("down", -5.0, BAND) == CORRECT


def test_long_is_treated_as_up():
    assert verdict_for("long", 4.0, BAND) == CORRECT
    assert verdict_for("short", 4.0, BAND) == INCORRECT


def test_move_inside_band_is_flat_regardless_of_direction():
    """Рух менший за смугу — ні перемога, ні поразка. Інакше шум у
    ±0.1% зараховувався б як влучання."""
    assert verdict_for("up", 1.9, BAND) == FLAT
    assert verdict_for("down", -1.9, BAND) == FLAT


def test_band_boundary_counts_as_move():
    assert verdict_for("up", 2.0, BAND) == CORRECT


def test_watch_is_no_claim_even_on_big_move():
    assert verdict_for("watch", 30.0, BAND) == NO_CLAIM


def test_missing_price_is_unavailable_and_wins_over_everything():
    """Відсутність котирування — наша проблема, не помилка агента, і
    перевіряється ПЕРШОЮ."""
    assert verdict_for("up", None, BAND) == UNAVAILABLE
    assert verdict_for("watch", None, BAND) == UNAVAILABLE


# ============================================================
# Фактичний рух
# ============================================================


def test_realized_pct_basic():
    assert realized_pct(100, 110) == pytest.approx(10.0)
    assert realized_pct(100, 90) == pytest.approx(-10.0)


def test_realized_pct_none_on_missing_or_zero():
    assert realized_pct(None, 100) is None
    assert realized_pct(100, None) is None
    assert realized_pct(0, 100) is None


# ============================================================
# Hit rate — що в знаменнику
# ============================================================


def test_hit_rate_counts_only_scored_verdicts():
    verdicts = [CORRECT, CORRECT, INCORRECT, FLAT, NO_CLAIM, UNAVAILABLE]
    # 2 із 3, а не 2 із 6
    assert hit_rate(verdicts) == pytest.approx(66.666, abs=0.01)


def test_hit_rate_none_when_nothing_scorable():
    assert hit_rate([FLAT, NO_CLAIM, UNAVAILABLE]) is None
    assert hit_rate([]) is None


# ============================================================
# Де шукати ціну
# ============================================================


def test_crypto_screening_rows_use_exchange_klines():
    """Ф'ючерсних цін у нашій БД немає взагалі — лише обсяг/funding/OI."""
    assert _venue_for_trading_list("crypto", "crypto_screening") == VENUE_BINANCE_FUTURES


def test_screening_rows_use_stock_quotes():
    assert _venue_for_trading_list("stock", "screening") == VENUE_DB_STOCK


def test_watchlist_rows_use_their_own_series():
    assert _venue_for_trading_list("commodity", "watchlist") == VENUE_DB_WATCHLIST
    assert _venue_for_trading_list("fx", "watchlist") == VENUE_DB_WATCHLIST


def test_watchlist_crypto_is_not_futures():
    """`crypto` з watchlist — це btc/eth/sol зі спота в БД, не
    ф'ючерсний символ."""
    assert _venue_for_trading_list("crypto", "watchlist") == VENUE_DB_WATCHLIST


def test_discovery_sources_route_to_their_price_venue():
    """Новачки в списку торгів (2026-10-22): крипта — ф'ючерсний
    символ, акція — котирування акцій. Інакше наслідок завжди
    'unavailable'."""
    assert _venue_for_trading_list("crypto", "discovery") == VENUE_BINANCE_FUTURES
    assert _venue_for_trading_list("stock", "discovery") == VENUE_DB_STOCK
    assert _venue_for_trading_list("stock", "watchlist") == VENUE_DB_STOCK
