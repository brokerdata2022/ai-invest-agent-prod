"""
Тести crypto_screening/aggregate_sources.py — чиста функція, синтетичні
знімки трьох бірж (без мережі/БД).
"""

from decimal import Decimal

from crypto_screening.aggregate_sources import (
    aggregate_snapshots,
    canonicalize_symbol,
    enrich_with_binance_oi,
)


def test_canonicalize_symbol_normalizes_okx_only():
    assert canonicalize_symbol("okx_futures", "BTC-USDT-SWAP") == "BTCUSDT"
    assert canonicalize_symbol("binance_futures", "BTCUSDT") == "BTCUSDT"
    assert canonicalize_symbol("bybit_futures", "BTCUSDT") == "BTCUSDT"


def test_aggregate_sums_volume_and_averages_rates_across_three_exchanges():
    binance = {"BTCUSDT": {"quote_volume": Decimal("5000000000"), "funding_rate": Decimal("0.0001")}}
    bybit = {
        "BTCUSDT": {
            "quote_volume": Decimal("2000000000"),
            "funding_rate": Decimal("0.0002"),
            "price_change_percent": Decimal("3.5"),
            "open_interest_value": Decimal("4000000000"),
        }
    }
    okx = {
        "BTC-USDT-SWAP": {
            "quote_volume": Decimal("3000000000"),
            "price_change_percent": Decimal("3.0"),
            "open_interest_usd": Decimal("2000000000"),
        }
    }

    result = aggregate_snapshots(binance, bybit, okx)

    assert set(result) == {"BTCUSDT"}
    btc = result["BTCUSDT"]
    assert btc["quote_volume_24h"] == Decimal("10000000000")  # 5+2+3 млрд
    assert btc["open_interest_usd"] == Decimal("6000000000")  # 4+2 млрд (без Binance)
    assert btc["funding_rate_avg"] == Decimal("0.00015")  # (0.0001+0.0002)/2
    assert btc["price_change_percent_avg"] == Decimal("3.25")  # (3.5+3.0)/2
    assert btc["exchange_count"] == 3
    assert btc["exchanges"] == ["binance_futures", "bybit_futures", "okx_futures"]


def test_aggregate_keeps_symbols_present_on_only_one_exchange():
    binance = {"OBSCUREUSDT": {"quote_volume": Decimal("1000"), "funding_rate": Decimal("0.001")}}

    result = aggregate_snapshots(binance, {}, {})

    assert result["OBSCUREUSDT"]["exchange_count"] == 1
    assert result["OBSCUREUSDT"]["open_interest_usd"] == Decimal(0)
    assert result["OBSCUREUSDT"]["price_change_percent_avg"] is None  # Binance snapshot його не дає


def test_enrich_with_binance_oi_adds_to_existing_symbol_only():
    aggregated = {
        "BTCUSDT": {"open_interest_usd": Decimal("6000000000")},
        "ETHUSDT": {"open_interest_usd": Decimal("2000000000")},
    }

    enrich_with_binance_oi(aggregated, {"BTCUSDT": Decimal("1000000000"), "UNKNOWNUSDT": Decimal("500")})

    assert aggregated["BTCUSDT"]["open_interest_usd"] == Decimal("7000000000")
    assert aggregated["ETHUSDT"]["open_interest_usd"] == Decimal("2000000000")  # не чіпалось
    assert "UNKNOWNUSDT" not in aggregated  # символа немає в aggregated -> ігнорується
