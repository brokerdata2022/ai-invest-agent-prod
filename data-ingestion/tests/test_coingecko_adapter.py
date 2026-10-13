"""
Тести CoinGeckoAdapter на збережених прикладах відповіді (JSON, реальні
відповіді `GET /coins/{bitcoin,kinesis-silver}/market_chart?vs_currency=usd&days=5&interval=daily`,
отримані живими запитами 2026-09-26) — жодних реальних мережевих викликів.

Обидві фікстури мають природний дублікат календарної дати (два записи
за 2026-09-26 — останній, "поточний", ще не завершена доба), корисний
для перевірки дедупу в normalize(). btc (`market_caps`) і xagusd
(`prices`) покривають дві різні гілки METRICS (різне поле відповіді,
різний суфікс metric_id — докладніше docstring адаптера).
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from crypto.coingecko_adapter import CoinGeckoAdapter

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def btc_response():
    with open(FIXTURES_DIR / "coingecko_bitcoin_market_chart_response.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def xagusd_response():
    with open(FIXTURES_DIR / "coingecko_kinesis_silver_market_chart_response.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def adapter():
    return CoinGeckoAdapter(metric_id="btc")


@pytest.fixture
def xagusd_adapter():
    return CoinGeckoAdapter(metric_id="xagusd")


def test_metric_id_resolves_to_coin_id(adapter):
    assert adapter.coin_id == "bitcoin"
    assert adapter.field_key == "market_caps"
    assert adapter.suffix == "market_cap"
    assert adapter.source == "coingecko"


def test_xagusd_resolves_to_kinesis_silver_prices(xagusd_adapter):
    assert xagusd_adapter.coin_id == "kinesis-silver"
    assert xagusd_adapter.field_key == "prices"
    assert xagusd_adapter.suffix == "close"


def test_xagusd_normalize_produces_close_metric_id(xagusd_adapter, xagusd_response):
    records = xagusd_adapter.normalize(xagusd_response)

    assert len(records) == 5  # той самий дедуп по даті, що й для btc
    assert {r.metric_id for r in records} == {"xagusd_close"}
    assert all(r.source == "coingecko" for r in records)

    record_25 = next(r for r in records if r.observed_at == date(2026, 9, 25))
    assert record_25.value == Decimal("63.87753400523615")


def test_unknown_metric_id_rejected():
    with pytest.raises(ValueError):
        CoinGeckoAdapter(metric_id="doge")


def test_normalize_dedupes_same_calendar_date(adapter, btc_response):
    records = adapter.normalize(btc_response)

    # Фікстура має 6 сирих точок, з яких дві за той самий день
    # (2026-09-26) — має лишитись 5 записів, по одному на дату.
    assert len(records) == 5
    dates = [r.observed_at for r in records]
    assert dates == sorted(dates)
    assert len(set(dates)) == 5


def test_normalize_keeps_latest_point_for_duplicated_date(adapter, btc_response):
    records = adapter.normalize(btc_response)
    record_26 = next(r for r in records if r.observed_at == date(2026, 9, 26))

    # Друга (пізніша) точка за 2026-09-26 має значення 1688455723561.4128,
    # не перша (1689021200315.1973) — перевіряємо, що лишилась саме вона.
    assert record_26.value == Decimal("1688455723561.4128")
    assert record_26.metric_id == "btc_market_cap"
    assert record_26.source == "coingecko"
    assert record_26.revision is None  # revision визначає common/db.py, не адаптер


def test_normalize_values_for_distinct_dates(adapter, btc_response):
    records = adapter.normalize(btc_response)
    record_22 = next(r for r in records if r.observed_at == date(2026, 9, 22))
    assert record_22.value == Decimal("1739520328890.6677")


def test_normalize_raises_gracefully_on_missing_market_caps(adapter):
    assert adapter.normalize({"prices": [], "total_volumes": []}) == []


def test_fetch_builds_expected_params(adapter):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"market_caps": []}

    class FakeSession:
        def get(self, url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse()

    adapter.session = FakeSession()
    adapter.fetch(limit=5)

    assert captured["url"] == "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
    assert captured["params"]["vs_currency"] == "usd"
    assert captured["params"]["days"] == 5
    assert captured["params"]["interval"] == "daily"


def test_fetch_defaults_days_when_no_limit(adapter):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"market_caps": []}

    class FakeSession:
        def get(self, url, params=None, timeout=None):
            captured["params"] = params
            return FakeResponse()

    adapter.session = FakeSession()
    adapter.fetch()

    assert captured["params"]["days"] == 30
