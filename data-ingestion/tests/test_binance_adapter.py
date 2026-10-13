"""
Тести BinanceAdapter на збереженому прикладі відповіді (JSON, реальна
відповідь `GET /api/v3/klines?symbol=BTCUSDT&interval=1d&limit=4`,
отримана живим запитом 2026-09-26) — жодних реальних мережевих викликів.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from crypto.binance_adapter import BinanceAdapter

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def btc_response():
    with open(FIXTURES_DIR / "binance_btcusdt_klines_response.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def adapter():
    return BinanceAdapter(metric_id="btc")


def test_metric_id_resolves_to_symbol(adapter):
    assert adapter.symbol == "BTCUSDT"
    assert adapter.source == "binance"


def test_unknown_metric_id_rejected():
    with pytest.raises(ValueError):
        BinanceAdapter(metric_id="doge")


def test_explicit_symbol_bypasses_metrics_dict():
    # 2026-10-03, docs/decisions.md: watchlist_add додає крипто поза
    # оригінальним закритим METRICS — symbol= обходить словник напряму.
    adapter = BinanceAdapter(metric_id="bnb", symbol="BNBUSDT")
    assert adapter.symbol == "BNBUSDT"
    assert adapter.metric_id == "bnb"


def test_normalize_produces_close_and_volume_per_day(adapter, btc_response):
    records = adapter.normalize(btc_response)

    assert len(records) == 8  # 4 свічки * 2 поля (close, volume)
    metric_ids = {r.metric_id for r in records}
    assert metric_ids == {"btc_close", "btc_volume"}

    close_records = [r for r in records if r.metric_id == "btc_close"]
    assert len(close_records) == 4


def test_normalize_values_and_dates(adapter, btc_response):
    records = adapter.normalize(btc_response)

    close_24 = next(
        r for r in records if r.metric_id == "btc_close" and r.observed_at == date(2026, 9, 24)
    )
    assert close_24.value == Decimal("84410.24000000")
    assert close_24.source == "binance"
    assert close_24.revision is None  # revision визначає common/db.py, не адаптер

    volume_24 = next(
        r for r in records if r.metric_id == "btc_volume" and r.observed_at == date(2026, 9, 24)
    )
    assert volume_24.value == Decimal("18703.61971000")


def test_normalize_raises_on_missing_values_handled_gracefully(adapter):
    # Неочікуваний формат (не список) — не падає, повертає порожній список
    assert adapter.normalize({"code": -1121, "msg": "Invalid symbol."}) == []


def test_fetch_builds_expected_params(adapter, monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    class FakeSession:
        def get(self, url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse()

    adapter.session = FakeSession()
    adapter.fetch(limit=4)

    assert captured["url"] == "https://api.binance.com/api/v3/klines"
    assert captured["params"]["symbol"] == "BTCUSDT"
    assert captured["params"]["interval"] == "1d"
    assert captured["params"]["limit"] == 4
