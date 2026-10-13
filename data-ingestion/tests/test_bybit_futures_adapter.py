"""
Тести crypto/bybit_futures_adapter.py на синтетичних відповідях,
побудованих за офіційною документацією Bybit v5 (жоден реальний
мережевий виклик — той самий підхід, що test_binance_futures_adapter.py).
"""

from decimal import Decimal

from crypto.bybit_futures_adapter import (
    BybitFuturesAdapter,
    fetch_market_snapshot,
    list_perpetual_symbols,
)

INSTRUMENTS_INFO_RESPONSE = {
    "retCode": 0,
    "result": {
        "category": "linear",
        "list": [
            {
                "symbol": "BTCUSDT",
                "status": "Trading",
                "contractType": "LinearPerpetual",
                "quoteCoin": "USDT",
                "launchTime": "1585526400000",  # 2020-03-30
            },
            {
                "symbol": "ETHUSDT",
                "status": "Trading",
                "contractType": "LinearPerpetual",
                "quoteCoin": "USDT",
                "launchTime": "1585526400000",
            },
            {  # інверсний контракт (quote=USD, не USDT) — виключити
                "symbol": "BTCUSD",
                "status": "Trading",
                "contractType": "InversePerpetual",
                "quoteCoin": "USD",
                "launchTime": "1585526400000",
            },
            {  # не Trading — виключити
                "symbol": "DELISTEDUSDT",
                "status": "Delisted",
                "contractType": "LinearPerpetual",
                "quoteCoin": "USDT",
                "launchTime": "1585526400000",
            },
        ],
    },
}

TICKERS_RESPONSE = {
    "retCode": 0,
    "result": {
        "category": "linear",
        "list": [
            {
                "symbol": "BTCUSDT",
                "turnover24h": "1200000000.50",
                "price24hPcnt": "0.035",
                "fundingRate": "0.0001",
                "openInterestValue": "500000000.00",
            },
            {
                "symbol": "ETHUSDT",
                "turnover24h": "600000000.25",
                "price24hPcnt": "-0.021",
                "fundingRate": "-0.025",
                "openInterestValue": "200000000.00",
            },
            {  # датований ф'ючерс (не perpetual) — живо виявлено 2026-09-27,
               # той самий bulk-ендпоінт віддає обидва типи, fundingRate порожній
                "symbol": "BTCUSDT-02OCT26",
                "turnover24h": "131012.65",
                "price24hPcnt": "0.005515",
                "fundingRate": "",
                "openInterestValue": "12065034.79",
            },
        ],
    },
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


def test_list_perpetual_symbols_filters_inverse_and_inactive():
    session = FakeSession({
        "https://api.bybit.com/v5/market/instruments-info": INSTRUMENTS_INFO_RESPONSE,
    })

    symbols = list_perpetual_symbols(session)

    assert {s["symbol"] for s in symbols} == {"BTCUSDT", "ETHUSDT"}
    btc = next(s for s in symbols if s["symbol"] == "BTCUSDT")
    assert btc["onboard_date"].isoformat() == "2020-03-30"


def test_fetch_market_snapshot_normalizes_price_change_percent_to_percent_units():
    session = FakeSession({"https://api.bybit.com/v5/market/tickers": TICKERS_RESPONSE})

    snapshot = fetch_market_snapshot(session)

    # BTCUSDT-02OCT26 (датований ф'ючерс, порожній fundingRate) виключений
    assert set(snapshot) == {"BTCUSDT", "ETHUSDT"}
    # Bybit сирий 0.035 (частка) -> нормалізовано в 3.5 (%), той самий стиль, що Binance
    assert snapshot["BTCUSDT"]["price_change_percent"] == Decimal("3.5")
    assert snapshot["BTCUSDT"]["open_interest_value"] == Decimal("500000000.00")
    assert snapshot["ETHUSDT"]["funding_rate"] == Decimal("-0.025")


def test_fetch_market_snapshot_filters_dated_futures_without_warning(caplog):
    session = FakeSession({"https://api.bybit.com/v5/market/tickers": TICKERS_RESPONSE})

    snapshot = fetch_market_snapshot(session)

    assert "BTCUSDT-02OCT26" not in snapshot
    assert not any("пропущено" in record.message for record in caplog.records)


def test_adapter_normalizes_snapshot_into_three_metrics_per_symbol():
    adapter = BybitFuturesAdapter(session=FakeSession({
        "https://api.bybit.com/v5/market/tickers": TICKERS_RESPONSE,
    }))

    records = adapter.collect()

    assert len(records) == 6  # 2 symbols * 3 metrics
    metric_ids = {r.metric_id for r in records}
    assert metric_ids == {
        "btcusdt_quote_volume", "btcusdt_funding_rate", "btcusdt_open_interest_value",
        "ethusdt_quote_volume", "ethusdt_funding_rate", "ethusdt_open_interest_value",
    }
    assert all(r.source == "bybit_futures" for r in records)
