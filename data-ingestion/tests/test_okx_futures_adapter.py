"""
Тести crypto/okx_futures_adapter.py на синтетичних відповідях,
побудованих за офіційною документацією OKX v5 (жоден реальний
мережевий виклик — той самий підхід, що інші два адаптери цього кроку).
"""

from decimal import Decimal

from crypto.okx_futures_adapter import (
    OkxFuturesAdapter,
    fetch_funding_rate,
    fetch_market_snapshot,
    list_perpetual_symbols,
)

INSTRUMENTS_RESPONSE = {
    "code": "0",
    "data": [
        {"instId": "BTC-USDT-SWAP", "state": "live", "listTime": "1611916800000"},  # 2021-01-29
        {"instId": "ETH-USDT-SWAP", "state": "live", "listTime": "1611916800000"},
        {"instId": "BTC-USD-SWAP", "state": "live", "listTime": "1611916800000"},  # coin-margined — виключити
        {"instId": "DELISTED-USDT-SWAP", "state": "suspend", "listTime": "1611916800000"},  # не live — виключити
    ],
}

TICKERS_RESPONSE = {
    "code": "0",
    "data": [
        # volCcy24h — БАЗОВА валюта (BTC/ETH), не USDT (живо виявлено
        # 2026-09-27) — реалістичні порядки величин, як у живому прогоні.
        {"instId": "BTC-USDT-SWAP", "last": "65000.0", "open24h": "63000.0", "volCcy24h": "14000.00"},
        {"instId": "ETH-USDT-SWAP", "last": "3200.0", "open24h": "3300.0", "volCcy24h": "125000.00"},
        {"instId": "NOINTERESTUSDT-SWAP", "last": "1.0", "open24h": "1.0", "volCcy24h": "10.00"},
    ],
}

OPEN_INTEREST_RESPONSE = {
    "code": "0",
    "data": [
        {"instId": "BTC-USDT-SWAP", "oiCcy": "12345.6789"},
        {"instId": "ETH-USDT-SWAP", "oiCcy": "98765.4321"},
    ],
}

FUNDING_RATE_RESPONSE = {
    "code": "0",
    "data": [{"instId": "BTC-USDT-SWAP", "fundingRate": "0.00015"}],
}


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

    def get(self, url, params=None, timeout=None):
        return FakeResponse(self.responses_by_url[url])


def test_list_perpetual_symbols_filters_coin_margined_and_inactive():
    session = FakeSession({"https://www.okx.com/api/v5/public/instruments": INSTRUMENTS_RESPONSE})

    symbols = list_perpetual_symbols(session)

    assert {s["symbol"] for s in symbols} == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
    btc = next(s for s in symbols if s["symbol"] == "BTC-USDT-SWAP")
    assert btc["onboard_date"].isoformat() == "2021-01-29"


def test_fetch_market_snapshot_computes_price_change_percent_and_joins_oi():
    session = FakeSession({
        "https://www.okx.com/api/v5/market/tickers": TICKERS_RESPONSE,
        "https://www.okx.com/api/v5/public/open-interest": OPEN_INTEREST_RESPONSE,
    })

    snapshot = fetch_market_snapshot(session)

    # NOINTERESTUSDT-SWAP є в tickers, але не в open-interest — виключений (join)
    assert set(snapshot) == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
    # (65000 - 63000) / 63000 * 100 ~= 3.1746%
    assert round(snapshot["BTC-USDT-SWAP"]["price_change_percent"], 2) == Decimal("3.17")
    # volCcy24h/oiCcy — базова валюта (живо виявлено 2026-09-27), не
    # USDT: 14000 BTC / 12345.6789 BTC (фікстура) * last 65000.0 -> $
    assert snapshot["BTC-USDT-SWAP"]["quote_volume"] == Decimal("14000.00") * Decimal("65000.0")
    assert snapshot["BTC-USDT-SWAP"]["open_interest_usd"] == Decimal("12345.6789") * Decimal("65000.0")
    # (3200 - 3300) / 3300 * 100 ~= -3.0303%
    assert round(snapshot["ETH-USDT-SWAP"]["price_change_percent"], 2) == Decimal("-3.03")


def test_fetch_funding_rate_returns_decimal():
    session = FakeSession({"https://www.okx.com/api/v5/public/funding-rate": FUNDING_RATE_RESPONSE})

    rate = fetch_funding_rate(session, "BTC-USDT-SWAP")

    assert rate == Decimal("0.00015")


def test_fetch_funding_rate_handles_empty_data():
    session = FakeSession({"https://www.okx.com/api/v5/public/funding-rate": {"code": "0", "data": []}})

    assert fetch_funding_rate(session, "UNKNOWN-USDT-SWAP") is None


def test_adapter_normalizes_snapshot_into_two_metrics_per_symbol():
    adapter = OkxFuturesAdapter(session=FakeSession({
        "https://www.okx.com/api/v5/market/tickers": TICKERS_RESPONSE,
        "https://www.okx.com/api/v5/public/open-interest": OPEN_INTEREST_RESPONSE,
    }))

    records = adapter.collect()

    assert len(records) == 4  # 2 symbols * 2 metrics (без funding_rate)
    metric_ids = {r.metric_id for r in records}
    assert metric_ids == {
        "btc-usdt-swap_quote_volume", "btc-usdt-swap_open_interest_usd",
        "eth-usdt-swap_quote_volume", "eth-usdt-swap_open_interest_usd",
    }
    assert all(r.source == "okx_futures" for r in records)
