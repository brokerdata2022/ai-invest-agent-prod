"""
Тести crypto/backfill_bybit_derivatives.py на синтетичних відповідях
(той самий підхід, що test_bybit_futures_adapter.py) — жоден реальний
мережевий виклик.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from crypto.backfill_bybit_derivatives import (
    build_records,
    fetch_daily_klines,
    fetch_daily_open_interest,
)

KLINE_RESPONSE = {
    "retCode": 0,
    "result": {
        "symbol": "BTCUSDT",
        "category": "linear",
        "list": [
            # [start_ts_ms, open, high, low, close, volume, turnover]
            ["1790899200000", "84836", "87242.2", "83835.6", "84100.1", "82623.407", "7088107749.4506"],
            ["1790812800000", "83573.8", "85249.6", "83107.7", "84836", "63131.965", "5305713748.8637"],
        ],
    },
}

OPEN_INTEREST_RESPONSE = {
    "retCode": 0,
    "result": {
        "symbol": "BTCUSDT",
        "category": "linear",
        "list": [
            {"openInterest": "56477.83100000", "timestamp": "1790899200000"},
            {"openInterest": "54488.31300000", "timestamp": "1790812800000"},
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


def test_fetch_daily_klines_parses_close_and_turnover():
    session = FakeSession({"https://api.bybit.com/v5/market/kline": KLINE_RESPONSE})

    klines = fetch_daily_klines(session, "BTCUSDT", days=2)

    assert set(klines) == {1790899200000, 1790812800000}
    assert klines[1790899200000]["close"] == Decimal("84100.1")
    assert klines[1790899200000]["turnover"] == Decimal("7088107749.4506")


def test_fetch_daily_open_interest_parses_base_currency_values():
    session = FakeSession({"https://api.bybit.com/v5/market/open-interest": OPEN_INTEREST_RESPONSE})

    oi = fetch_daily_open_interest(session, "BTCUSDT", days=2)

    assert oi[1790899200000] == Decimal("56477.83100000")


def test_build_records_converts_oi_to_usd_and_keeps_volume_as_is():
    klines = {1790899200000: {"close": Decimal("100"), "turnover": Decimal("999")}}
    open_interest = {1790899200000: Decimal("50")}
    fetched_at = datetime(2026, 10, 2, tzinfo=timezone.utc)

    records = build_records("BTCUSDT", klines, open_interest, fetched_at)

    by_metric = {r.metric_id: r for r in records}
    assert by_metric["btcusdt_quote_volume"].value == Decimal("999")
    # 50 контрактів * 100$ close = 5000$ open_interest_value -- той самий
    # принцип юніт-нормалізації, що вже в OKX-адаптері (docs/decisions.md).
    assert by_metric["btcusdt_open_interest_value"].value == Decimal("5000")
    assert by_metric["btcusdt_quote_volume"].observed_at.isoformat() == "2026-10-02"


def test_build_records_skips_open_interest_when_no_matching_timestamp():
    klines = {1790899200000: {"close": Decimal("100"), "turnover": Decimal("999")}}
    open_interest = {}  # символ щойно лістингований -- ще немає OI-історії
    fetched_at = datetime(2026, 10, 2, tzinfo=timezone.utc)

    records = build_records("BTCUSDT", klines, open_interest, fetched_at)

    metric_ids = {r.metric_id for r in records}
    assert metric_ids == {"btcusdt_quote_volume"}


def test_build_records_skips_days_already_covered_by_daily_job():
    # Живий баг (2026-10-02): день, що щоденна джоба вже зібрала,
    # НЕ повинен отримати нову ревізію з іншої методології вимірювання
    # (денна свічка-close замість живого знімка) -- covered_*_dates
    # мають перекрити ОБИДВІ метрики незалежно.
    klines = {
        1790899200000: {"close": Decimal("100"), "turnover": Decimal("999")},  # 2026-10-02
        1790812800000: {"close": Decimal("90"), "turnover": Decimal("500")},   # 2026-10-01 (новий день)
    }
    open_interest = {
        1790899200000: Decimal("50"),
        1790812800000: Decimal("40"),
    }
    fetched_at = datetime(2026, 10, 2, tzinfo=timezone.utc)
    already_covered = frozenset({date(2026, 10, 2)})

    records = build_records(
        "BTCUSDT", klines, open_interest, fetched_at,
        covered_volume_dates=already_covered, covered_oi_dates=already_covered,
    )

    observed_dates = {r.observed_at for r in records}
    assert observed_dates == {date(2026, 10, 1)}
    assert len(records) == 2  # лише quote_volume + open_interest_value за 10-01
