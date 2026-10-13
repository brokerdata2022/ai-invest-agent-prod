"""
Тести TwelveDataAdapter на збереженому прикладі відповіді (JSON,
фрагмент реальної відповіді, отриманої живим запитом користувача
2026-09-20) — жодних реальних мережевих викликів.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from quotes.twelvedata_adapter import TwelveDataAdapter, search_symbol

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def aapl_response():
    with open(FIXTURES_DIR / "twelvedata_aapl_response.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def adapter():
    return TwelveDataAdapter(api_key="test-key", ticker="aapl")


def test_ticker_normalized_to_uppercase(adapter):
    assert adapter.ticker == "AAPL"
    assert adapter.source == "twelvedata"


def test_missing_api_key_rejected():
    with pytest.raises(ValueError):
        TwelveDataAdapter(api_key="", ticker="AAPL")


def test_invalid_ticker_rejected():
    with pytest.raises(ValueError):
        TwelveDataAdapter(api_key="test-key", ticker="")
    with pytest.raises(ValueError):
        TwelveDataAdapter(api_key="test-key", ticker="   ")


def test_normalize_produces_close_and_volume_per_day(adapter, aapl_response):
    records = adapter.normalize(aapl_response)

    # 3 дні * 2 поля (close, volume) - 1 пропущений volume (порожній
    # рядок у 3-му запису фікстури) = 5
    assert len(records) == 5

    metric_ids = {r.metric_id for r in records}
    assert metric_ids == {"aapl_close", "aapl_volume"}

    close_records = [r for r in records if r.metric_id == "aapl_close"]
    assert len(close_records) == 3


def test_normalize_values_and_dates(adapter, aapl_response):
    records = adapter.normalize(aapl_response)
    close_18 = next(
        r for r in records if r.metric_id == "aapl_close" and r.observed_at == date(2026, 9, 18)
    )
    assert close_18.value == Decimal("336.13000")
    assert close_18.source == "twelvedata"
    assert close_18.revision is None  # revision визначає common/db.py, не адаптер

    volume_18 = next(
        r for r in records if r.metric_id == "aapl_volume" and r.observed_at == date(2026, 9, 18)
    )
    assert volume_18.value == Decimal("86433100")


def test_normalize_strips_slash_from_forex_commodity_ticker(aapl_response):
    # Regression: Twelve Data вимагає слеш у символі forex/commodity
    # ("XAU/USD" — twelvedata.com/docs, перевірено 2026-09-26), але
    # metric_id має лишатись без слеша, узгодженим з внутрішнім
    # asset_id ("xauusd", news/queries.py:WATCHLIST_ASSET_IDS) — не
    # форматом символу конкретного джерела.
    gold_adapter = TwelveDataAdapter(api_key="test-key", ticker="XAU/USD")
    assert gold_adapter.ticker == "XAU/USD"  # для запиту до API -- як є

    records = gold_adapter.normalize(aapl_response)
    metric_ids = {r.metric_id for r in records}
    assert metric_ids == {"xauusd_close", "xauusd_volume"}


def test_normalize_uses_explicit_metric_id_override(aapl_response):
    # 2026-10-04: коли /watchlist_add резолвнув тикер через
    # symbol_search (напр. "NATGAS" -> "NG"), watchlist_assets.metric_id
    # лишається на ОРИГІНАЛЬНОМУ asset_id ("natgas_close"), не на
    # резолвленому тикері — orchestration/jobs.py:_watchlist_prices
    # передає metric_id= явно (той самий принцип, що BinanceAdapter),
    # щоб зібрана ціна лягала під ТОЙ САМИЙ metric_id, що читає
    # fetch_price_sources()/watchlist_notify.py, а не під виведений з
    # тикера "ng_close", який ніхто з них не шукає.
    adapter = TwelveDataAdapter(api_key="test-key", ticker="NG", metric_id="natgas")
    records = adapter.normalize(aapl_response)
    metric_ids = {r.metric_id for r in records}
    assert metric_ids == {"natgas_close", "natgas_volume"}


def test_normalize_skips_empty_volume(adapter, aapl_response):
    records = adapter.normalize(aapl_response)
    day_16_metric_ids = {
        r.metric_id for r in records if r.observed_at == date(2026, 9, 16)
    }
    assert day_16_metric_ids == {"aapl_close"}  # volume порожній рядок цього дня


def test_normalize_raises_on_api_error_payload(adapter):
    error_response = {
        "code": 401,
        "message": "Invalid API key",
        "status": "error",
    }
    with pytest.raises(ValueError):
        adapter.normalize(error_response)


def test_normalize_raises_on_missing_values(adapter):
    with pytest.raises(ValueError):
        adapter.normalize({"meta": {}, "status": "ok"})


def test_fetch_builds_expected_params(adapter, monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"values": [], "status": "ok"}

    class FakeSession:
        def get(self, url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse()

    adapter.session = FakeSession()
    adapter.fetch(limit=5, observation_start="2026-01-01", observation_end="2026-08-01")

    assert captured["url"] == "https://api.twelvedata.com/time_series"
    assert captured["params"]["symbol"] == "AAPL"
    assert captured["params"]["interval"] == "1day"
    assert captured["params"]["apikey"] == "test-key"
    assert captured["params"]["outputsize"] == 5
    assert captured["params"]["start_date"] == "2026-01-01"
    assert captured["params"]["end_date"] == "2026-08-01"


# --- search_symbol() (2026-10-03, живий кейс "NATGAS" — watchlist_add) ---

class _FakeSearchResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSearchSession:
    def __init__(self, payload):
        self._payload = payload
        self.captured = {}

    def get(self, url, params=None, timeout=None):
        self.captured["url"] = url
        self.captured["params"] = params
        return _FakeSearchResponse(self._payload)


def test_search_symbol_returns_first_match():
    session = _FakeSearchSession(
        {"data": [{"symbol": "NG", "instrument_name": "Natural Gas Futures"}]}
    )
    result = search_symbol("natural gas", api_key="test-key", session=session)

    assert result == "NG"
    assert session.captured["params"] == {"symbol": "natural gas", "apikey": "test-key"}


def test_search_symbol_returns_none_when_no_matches():
    session = _FakeSearchSession({"data": []})
    assert search_symbol("totallyfakeasset", api_key="test-key", session=session) is None


def test_search_symbol_returns_none_when_data_key_missing():
    session = _FakeSearchSession({})
    assert search_symbol("x", api_key="test-key", session=session) is None


def test_search_symbol_returns_none_on_request_error(monkeypatch):
    import requests

    class _FailingSession:
        def get(self, url, params=None, timeout=None):
            raise requests.exceptions.ConnectionError("boom")

    assert search_symbol("x", api_key="test-key", session=_FailingSession()) is None
