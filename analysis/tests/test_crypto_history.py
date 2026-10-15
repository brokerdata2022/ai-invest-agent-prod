"""
Тести crypto_screening/history.py — зведення історії кількох бірж в
один денний ряд (чисті функції, без БД) і метрик-мапи
`_history_db.metric_pairs()`.

Головне, що тут перевіряється — НЕ арифметика (вона тривіальна), а
правило узгодженого набору бірж: наївна сума дала б фальшивий рух там,
де змінилось ПОКРИТТЯ, а не потік (docs/decisions.md 2026-10-15).
"""

from datetime import date, timedelta

import pytest

from crypto_screening._history_db import KIND_OI, KIND_VOLUME, metric_pairs
from crypto_screening.history import (
    consistent_daily_series,
    oi_change_pct,
    volume_spike_pct,
)


def _d(i: int) -> date:
    """i днів НАЗАД від фіксованої дати."""
    return date(2026, 10, 15) - timedelta(days=i)


# ============================================================
# consistent_daily_series
# ============================================================


def test_sums_across_exchanges_per_day():
    rows = [
        (_d(0), "bybit_futures", 60.0), (_d(0), "okx_futures", 40.0),
        (_d(1), "bybit_futures", 50.0), (_d(1), "okx_futures", 50.0),
    ]
    series = consistent_daily_series(rows, limit=2)

    assert series == [(_d(0), 100.0), (_d(1), 100.0)]


def test_newest_day_first():
    rows = [
        (_d(1), "bybit_futures", 10.0),
        (_d(0), "bybit_futures", 20.0),
    ]
    series = consistent_daily_series(rows, limit=2)

    assert [day for day, _ in series] == [_d(0), _d(1)]


def test_empty_when_window_shorter_than_limit():
    rows = [(_d(0), "bybit_futures", 10.0), (_d(1), "bybit_futures", 10.0)]

    assert consistent_daily_series(rows, limit=8) == []


def test_ignores_exchange_that_appeared_mid_window():
    """Лістинг з'явився в середині вікна — його НЕ беремо, інакше сума
    стрибне на розмір нової частки й це прочитається як приріст OI."""
    rows = [
        (_d(0), "bybit_futures", 100.0), (_d(0), "okx_futures", 900.0),  # новий лістинг
        (_d(1), "bybit_futures", 100.0), (_d(1), "okx_futures", 900.0),
        (_d(2), "bybit_futures", 100.0),                                 # на краю OKX ще немає
    ]
    series = consistent_daily_series(rows, limit=3)

    assert series == [(_d(0), 100.0), (_d(1), 100.0), (_d(2), 100.0)]
    assert oi_change_pct(series) == pytest.approx(0.0)  # руху не було — і не вигадали


def test_single_exchange_behaves_exactly_as_before():
    """Символ з однією біржею — поведінка та сама, що до правки:
    гірше стати не може."""
    rows = [(_d(i), "bybit_futures", 100.0 + i) for i in range(3)]
    series = consistent_daily_series(rows, limit=3)

    assert oi_change_pct(series) == pytest.approx((100.0 - 102.0) / 102.0 * 100)


def test_empty_when_no_common_exchange_on_window_edges():
    rows = [
        (_d(0), "okx_futures", 100.0),
        (_d(1), "bybit_futures", 100.0),
    ]

    assert consistent_daily_series(rows, limit=2) == []


# ============================================================
# Арифметика
# ============================================================


def test_oi_change_pct_none_on_zero_base():
    series = [(_d(0), 10.0), (_d(1), 0.0)]

    assert oi_change_pct(series) is None


def test_oi_change_pct_none_on_empty_series():
    assert oi_change_pct([]) is None
    assert volume_spike_pct([]) is None


def test_volume_spike_pct_latest_against_baseline_average():
    series = [(_d(0), 200.0)] + [(_d(i), 100.0) for i in range(1, 8)]

    assert volume_spike_pct(series) == pytest.approx(100.0)


# ============================================================
# metric_pairs — написання символу й назва метрики по біржах
# ============================================================


def test_metric_pairs_oi_uses_each_exchange_own_naming():
    assert metric_pairs("BTCUSDT", KIND_OI) == (
        ("binance_futures", "btcusdt_open_interest_usd"),
        ("bybit_futures", "btcusdt_open_interest_value"),
        ("okx_futures", "btc-usdt-swap_open_interest_usd"),
    )


def test_metric_pairs_volume_same_suffix_everywhere():
    assert metric_pairs("BTCUSDT", KIND_VOLUME) == (
        ("binance_futures", "btcusdt_quote_volume"),
        ("bybit_futures", "btcusdt_quote_volume"),
        ("okx_futures", "btc-usdt-swap_quote_volume"),
    )


def test_metric_pairs_skips_okx_for_non_usdt_symbol():
    """OKX збирає лише USDT-SWAP — питати базу про BTCUSDC там нема сенсу."""
    pairs = metric_pairs("BTCUSDC", KIND_OI)

    assert [source for source, _ in pairs] == ["binance_futures", "bybit_futures"]


def test_metric_pairs_rejects_unknown_kind():
    with pytest.raises(ValueError):
        metric_pairs("BTCUSDT", "funding")
