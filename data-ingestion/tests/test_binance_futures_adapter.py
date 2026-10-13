"""
Тести crypto/binance_futures_adapter.py на синтетичних відповідях,
побудованих за офіційною документацією Binance Futures API (жоден
реальний мережевий виклик; на відміну від test_binance_adapter.py тут
немає збереженої "живої" фікстури — перший реальний запит цього
адаптера користувач ще не підтвердив, докладніше docstring модуля).
"""

from datetime import date
from decimal import Decimal

import requests

from crypto.binance_futures_adapter import (
    BinanceFuturesAdapter,
    fetch_klines,
    fetch_market_snapshot,
    fetch_open_interest,
    list_perpetual_symbols,
)

EXCHANGE_INFO_RESPONSE = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
            "onboardDate": 1569398400000,  # 2019-09-25
        },
        {
            "symbol": "ETHUSDT",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
            "onboardDate": 1569398400000,
        },
        {  # не perpetual — виключити
            "symbol": "BTCUSDT_240927",
            "status": "TRADING",
            "contractType": "CURRENT_QUARTER",
            "quoteAsset": "USDT",
            "onboardDate": 1569398400000,
        },
        {  # не USDT — виключити
            "symbol": "BTCUSD_PERP",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USD",
            "onboardDate": 1569398400000,
        },
        {  # не TRADING — виключити
            "symbol": "DELISTEDUSDT",
            "status": "BREAK",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
            "onboardDate": 1569398400000,
        },
    ]
}

TICKER_24HR_RESPONSE = [
    {"symbol": "BTCUSDT", "quoteVolume": "1500000000.00", "priceChangePercent": "3.50"},
    {"symbol": "ETHUSDT", "quoteVolume": "800000000.00", "priceChangePercent": "-2.10"},
    {"symbol": "NOFUNDINGUSDT", "quoteVolume": "100.00", "priceChangePercent": "0.00"},
]

PREMIUM_INDEX_RESPONSE = [
    {"symbol": "BTCUSDT", "markPrice": "65000.10", "lastFundingRate": "0.00010000"},
    {"symbol": "ETHUSDT", "markPrice": "3200.50", "lastFundingRate": "-0.02500000"},
]


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses_by_url):
        self.responses_by_url = responses_by_url
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return FakeResponse(self.responses_by_url[url])


def test_list_perpetual_symbols_filters_non_perpetual_non_usdt_and_inactive():
    session = FakeSession({"https://fapi.binance.com/fapi/v1/exchangeInfo": EXCHANGE_INFO_RESPONSE})

    symbols = list_perpetual_symbols(session)

    assert {s["symbol"] for s in symbols} == {"BTCUSDT", "ETHUSDT"}
    btc = next(s for s in symbols if s["symbol"] == "BTCUSDT")
    assert btc["onboard_date"].isoformat() == "2019-09-25"


def test_fetch_market_snapshot_joins_ticker_and_funding_by_symbol():
    session = FakeSession({
        "https://fapi.binance.com/fapi/v1/ticker/24hr": TICKER_24HR_RESPONSE,
        "https://fapi.binance.com/fapi/v1/premiumIndex": PREMIUM_INDEX_RESPONSE,
    })

    snapshot = fetch_market_snapshot(session)

    # NOFUNDINGUSDT є в ticker, але не в premiumIndex — виключений (join)
    assert set(snapshot) == {"BTCUSDT", "ETHUSDT"}
    assert snapshot["BTCUSDT"]["quote_volume"] == Decimal("1500000000.00")
    assert snapshot["BTCUSDT"]["funding_rate"] == Decimal("0.00010000")
    assert snapshot["ETHUSDT"]["funding_rate"] == Decimal("-0.02500000")


def test_fetch_open_interest_returns_decimal():
    session = FakeSession({
        "https://fapi.binance.com/fapi/v1/openInterest": {"symbol": "BTCUSDT", "openInterest": "45000.500"},
    })

    oi = fetch_open_interest(session, "BTCUSDT")

    assert oi == Decimal("45000.500")


def test_fetch_open_interest_handles_malformed_response():
    session = FakeSession({
        "https://fapi.binance.com/fapi/v1/openInterest": {"code": -1121, "msg": "Invalid symbol."},
    })

    assert fetch_open_interest(session, "UNKNOWNUSDT") is None


def test_adapter_normalizes_snapshot_into_quote_volume_and_funding_rate_records():
    adapter = BinanceFuturesAdapter(session=FakeSession({
        "https://fapi.binance.com/fapi/v1/ticker/24hr": TICKER_24HR_RESPONSE,
        "https://fapi.binance.com/fapi/v1/premiumIndex": PREMIUM_INDEX_RESPONSE,
    }))

    records = adapter.collect()

    assert len(records) == 4  # 2 symbols * 2 metrics
    metric_ids = {r.metric_id for r in records}
    assert metric_ids == {"btcusdt_quote_volume", "btcusdt_funding_rate", "ethusdt_quote_volume", "ethusdt_funding_rate"}
    assert all(r.source == "binance_futures" for r in records)

    funding = next(r for r in records if r.metric_id == "ethusdt_funding_rate")
    assert funding.value == Decimal("-0.02500000")
    assert funding.revision is None


KLINES_RESPONSE = [
    # [openTime, open, high, low, close, volume, ...]
    [1758758400000, "60000.0", "61000.0", "59500.0", "60500.00", "1000"],  # 2025-09-25
    [1758844800000, "60500.0", "62000.0", "60200.0", "61800.00", "1200"],  # 2025-09-26
    [1758931200000, "61800.0", "63000.0", "61500.0", "62900.00", "1500"],  # 2025-09-27
]


def test_fetch_klines_returns_chronological_close_history():
    session = FakeSession({"https://fapi.binance.com/fapi/v1/klines": KLINES_RESPONSE})

    closes = fetch_klines(session, "BTCUSDT", limit=3)

    assert len(closes) == 3
    assert closes[0]["value"] == 60500.00
    assert closes[0]["volume"] == 1000.0
    assert closes[0]["observed_at"] == date(2025, 9, 25)
    assert closes[-1]["value"] == 62900.00
    assert closes[-1]["volume"] == 1500.0
    assert closes[-1]["observed_at"] == date(2025, 9, 27)


def test_fetch_klines_passes_through_requested_interval():
    session = FakeSession({"https://fapi.binance.com/fapi/v1/klines": KLINES_RESPONSE})

    fetch_klines(session, "BTCUSDT", limit=3, interval="4h")

    _, params = session.calls[-1]
    assert params["interval"] == "4h"


def test_fetch_klines_handles_unexpected_response_gracefully():
    session = FakeSession({"https://fapi.binance.com/fapi/v1/klines": {"code": -1121, "msg": "Invalid symbol."}})

    assert fetch_klines(session, "UNKNOWNUSDT") == []


class Failing400Response:
    def raise_for_status(self):
        raise requests.exceptions.HTTPError("400 Client Error: Bad Request")

    def json(self):
        raise AssertionError("не має викликатись — raise_for_status() падає раніше")


class Failing400Session:
    def get(self, url, params=None, timeout=None):
        return Failing400Response()


def test_fetch_klines_returns_empty_when_symbol_not_listed_on_binance():
    # Живо виявлено 2026-09-27: символ, узгоджений через Bybit/OKX
    # (aggregate_sources.py), не завжди є на Binance Futures під тією
    # самою назвою — Binance повертає HTTP 400, не порожній список.
    assert fetch_klines(Failing400Session(), "CASHCATUSDT") == []
