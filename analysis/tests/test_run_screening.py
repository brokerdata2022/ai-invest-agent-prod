"""
Тести run_screening.py — чисті обчислення (_pct_change,
compute_oi_change_pct/compute_volume_spike_pct/compute_price_indicators)
на monkeypatch-нутих БД/мережевих функціях, і наскрізний тест
run_screening() з повністю підміненими джерелами (без реальної БД/мережі,
той самий підхід, що test_compare_releases.py/test_update_forecasts.py).
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from crypto_screening.run_screening import (
    _pct_change,
    compute_monitoring_indicators,
    compute_oi_change_pct,
    compute_price_indicators,
    compute_volume_spike_pct,
    run_screening,
)


def test_pct_change_basic():
    assert _pct_change(100.0, 110.0) == pytest.approx(10.0)
    assert _pct_change(100.0, 90.0) == pytest.approx(-10.0)


def test_pct_change_zero_old_returns_none():
    assert _pct_change(0.0, 10.0) is None


def test_compute_oi_change_pct_returns_none_when_not_enough_history(monkeypatch):
    monkeypatch.setattr(
        "crypto_screening.run_screening.fetch_recent",
        lambda conn, source, metric_id, limit: [{"value": 100.0}],
    )
    assert compute_oi_change_pct(conn=None, symbol="BTCUSDT", window_days=7) is None


def test_compute_oi_change_pct_computes_from_newest_and_oldest(monkeypatch):
    rows = [{"value": 120.0}, {"value": 110.0}, {"value": 100.0}]  # найновіше перше
    monkeypatch.setattr(
        "crypto_screening.run_screening.fetch_recent",
        lambda conn, source, metric_id, limit: rows,
    )
    result = compute_oi_change_pct(conn=None, symbol="BTCUSDT", window_days=2)
    assert result == pytest.approx(20.0)  # (120-100)/100*100


def test_compute_volume_spike_pct_compares_latest_to_average_baseline(monkeypatch):
    rows = [{"value": 200.0}] + [{"value": 100.0}] * 7
    monkeypatch.setattr(
        "crypto_screening.run_screening.fetch_recent",
        lambda conn, source, metric_id, limit: rows,
    )
    result = compute_volume_spike_pct(conn=None, symbol="BTCUSDT")
    assert result == pytest.approx(100.0)  # 200 проти базового середнього 100 -> +100%


def test_compute_price_indicators_returns_none_when_not_enough_klines(monkeypatch):
    monkeypatch.setattr(
        "crypto_screening.run_screening.fetch_klines",
        lambda session, symbol, limit: [{"value": 1.0, "observed_at": date(2026, 1, 1)}] * 5,
    )
    result = compute_price_indicators(session=None, symbol="BTCUSDT")

    assert result == {"rsi": None, "trend_confirmed": False}


def test_compute_price_indicators_computes_rsi_and_trend_confirmed(monkeypatch):
    closes = [
        {"value": 100.0 + i, "observed_at": date(2026, 1, 1) + timedelta(days=i)}
        for i in range(30)
    ]
    monkeypatch.setattr(
        "crypto_screening.run_screening.fetch_klines",
        lambda session, symbol, limit: closes,
    )
    result = compute_price_indicators(session=None, symbol="BTCUSDT")

    assert result["rsi"] == 100.0  # монотонне зростання -> RSI=100 (test_indicators.py)
    assert result["trend_confirmed"] is True  # ідеально лінійний ряд б'є naive


def test_compute_monitoring_indicators_returns_none_when_not_enough_candles(monkeypatch):
    captured = {}

    def fake_fetch_klines(session, symbol, limit, interval):
        captured["interval"] = interval
        captured["limit"] = limit
        return [{"value": 1.0, "volume": 10.0, "observed_at": date(2026, 1, 1)}] * 5

    monkeypatch.setattr("crypto_screening.run_screening.fetch_klines", fake_fetch_klines)
    result = compute_monitoring_indicators(session=None, symbol="BTCUSDT")

    assert result == {"rsi": None, "volume_spike_pct": None}
    # Коротший ТФ (4г), не денний (2026-10-03, config.py:MONITORING_KLINE_INTERVAL)
    assert captured["interval"] == "4h"


def test_compute_monitoring_indicators_computes_rsi_and_volume_spike(monkeypatch):
    # 20 свічок однакового обсягу (10.0), остання -- сплеск у 10 разів.
    candles = [
        {"value": 100.0 + i, "volume": 10.0, "observed_at": date(2026, 1, 1) + timedelta(hours=4 * i)}
        for i in range(19)
    ]
    candles.append({"value": 119.0, "volume": 100.0, "observed_at": date(2026, 1, 4)})
    monkeypatch.setattr("crypto_screening.run_screening.fetch_klines", lambda session, symbol, limit, interval: candles)

    result = compute_monitoring_indicators(session=None, symbol="BTCUSDT")

    assert result["rsi"] == 100.0  # монотонне зростання -> RSI=100 (той самий, що денний тест)
    assert result["volume_spike_pct"] == pytest.approx(900.0)  # 10 -> 100, +900%


BINANCE_SYMBOLS = [{"symbol": "BTCUSDT", "onboard_date": date(2019, 1, 1)}]
BYBIT_SYMBOLS = [{"symbol": "BTCUSDT", "onboard_date": date(2019, 1, 1)}]
OKX_SYMBOLS = [{"symbol": "BTC-USDT-SWAP", "onboard_date": date(2019, 1, 1)}]

BINANCE_SNAPSHOT = {
    "BTCUSDT": {
        "quote_volume": Decimal("20000000"),
        "funding_rate": Decimal("0.0001"),
        "price_change_percent": Decimal("1.0"),
        "mark_price": Decimal("100"),
    }
}
BYBIT_SNAPSHOT = {
    "BTCUSDT": {
        "quote_volume": Decimal("20000000"),
        "funding_rate": Decimal("0.0001"),
        "price_change_percent": Decimal("1.0"),
        "open_interest_value": Decimal("6000000"),
    }
}
OKX_SNAPSHOT = {
    "BTC-USDT-SWAP": {
        "quote_volume": Decimal("20000000"),
        "price_change_percent": Decimal("1.0"),
        "open_interest_usd": Decimal("1000000"),
    }
}


class FakeConn:
    def close(self):
        pass


def _patch_sources(monkeypatch):
    monkeypatch.setattr("crypto_screening.run_screening.list_binance_symbols", lambda session: BINANCE_SYMBOLS)
    monkeypatch.setattr("crypto_screening.run_screening.list_bybit_symbols", lambda session: BYBIT_SYMBOLS)
    monkeypatch.setattr("crypto_screening.run_screening.list_okx_symbols", lambda session: OKX_SYMBOLS)
    monkeypatch.setattr("crypto_screening.run_screening.fetch_binance_snapshot", lambda session: BINANCE_SNAPSHOT)
    monkeypatch.setattr("crypto_screening.run_screening.fetch_bybit_snapshot", lambda session: BYBIT_SNAPSHOT)
    monkeypatch.setattr("crypto_screening.run_screening.fetch_okx_snapshot", lambda session: OKX_SNAPSHOT)
    monkeypatch.setattr("crypto_screening.run_screening.fetch_binance_oi", lambda session, symbol: Decimal("50000"))
    monkeypatch.setattr("crypto_screening.run_screening.get_connection", lambda: FakeConn())
    # save_long_run() пише в БД через conn.cursor() — FakeConn вище не
    # реалізує cursor() (той самий принцип, що upsert_candidate, який
    # теж не торкається реальної БД у цих тестах, лише перевіряють
    # саму логіку сигналів, не персистування).
    monkeypatch.setattr("crypto_screening.run_screening.save_long_run", lambda conn, signals: len(signals))
    monkeypatch.setattr("crypto_screening.run_screening.upsert_candidate", lambda *args, **kwargs: None)


def test_run_screening_wires_tier_a_survivor_into_long_signal(monkeypatch):
    _patch_sources(monkeypatch)
    monkeypatch.setattr(
        "crypto_screening.run_screening.compute_price_indicators",
        lambda session, symbol: {"rsi": 60.0, "trend_confirmed": True},
    )
    # OI росте (>0) для LONG-вікна, волатильність обсягу байдужа тут
    monkeypatch.setattr(
        "crypto_screening.run_screening.compute_oi_change_pct",
        lambda conn, symbol, window_days: 8.0,
    )
    monkeypatch.setattr("crypto_screening.run_screening.compute_volume_spike_pct", lambda conn, symbol: 0.0)

    results = run_screening()

    assert [s.symbol for s in results["long"]] == ["BTCUSDT"]
    assert results["short"] == []
    # price_change_percent=1.0 в усіх фікстурах-знімках -> pump_pct=1.0 < PUMP_THRESHOLD_PCT -> "none"
    assert results["watch"] == []


def test_run_screening_produces_watch_not_short_on_critical_funding_after_pump(monkeypatch):
    _patch_sources(monkeypatch)
    monkeypatch.setattr(
        "crypto_screening.run_screening.compute_price_indicators",
        lambda session, symbol: {"rsi": 78.0, "trend_confirmed": False},
    )
    monkeypatch.setattr(
        "crypto_screening.run_screening.compute_oi_change_pct",
        lambda conn, symbol, window_days: -10.0,
    )
    monkeypatch.setattr("crypto_screening.run_screening.compute_volume_spike_pct", lambda conn, symbol: 50.0)

    # Живий фікс 2026-09-27: pump_pct тепер береться з агрегованого
    # price_change_percent_avg (не з Binance klines) — підміняємо його
    # тут разом із funding_rate, щоб симулювати "памп + критичний
    # funding" (Bybit+Binance по 0.0001 усередньому не дасть ні того, ні іншого).
    critical_bybit = {
        **BYBIT_SNAPSHOT,
        "BTCUSDT": {**BYBIT_SNAPSHOT["BTCUSDT"], "funding_rate": Decimal("-0.05"), "price_change_percent": Decimal("45.0")},
    }
    critical_binance = {
        **BINANCE_SNAPSHOT,
        "BTCUSDT": {**BINANCE_SNAPSHOT["BTCUSDT"], "funding_rate": Decimal("-0.05"), "price_change_percent": Decimal("45.0")},
    }
    critical_okx = {
        **OKX_SNAPSHOT,
        "BTC-USDT-SWAP": {**OKX_SNAPSHOT["BTC-USDT-SWAP"], "price_change_percent": Decimal("45.0")},
    }
    monkeypatch.setattr("crypto_screening.run_screening.fetch_bybit_snapshot", lambda session: critical_bybit)
    monkeypatch.setattr("crypto_screening.run_screening.fetch_binance_snapshot", lambda session: critical_binance)
    monkeypatch.setattr("crypto_screening.run_screening.fetch_okx_snapshot", lambda session: critical_okx)

    results = run_screening()

    assert results["short"] == []
    assert [s.symbol for s in results["watch"]] == ["BTCUSDT"]
    assert results["long"] == []  # trend_confirmed=False -> не кваліфікується


def test_run_screening_detects_watch_from_single_exchange_pump_despite_stale_others(monkeypatch):
    # Регресія на живий баг 2026-09-27 (двічі): (1) WATCH не повинен
    # залежати від RSI/тренду (Binance klines) — лише від pump_pct +
    # funding rate; rsi=None/trend_confirmed=False симулює символ без
    # повної Binance-історії. (2) pump_pct = МАКСИМУМ серед бірж, не
    # середнє — Binance/OKX тут лишаються на "тихих" 1.0% (з фікстур),
    # а реальний памп бачить лише Bybit (83%, як живий приклад
    # користувача) — середнє (83+1+1)/3=28.3% не пройшло б поріг 30%,
    # максимум (83%) — проходить.
    _patch_sources(monkeypatch)
    monkeypatch.setattr(
        "crypto_screening.run_screening.compute_price_indicators",
        lambda session, symbol: {"rsi": None, "trend_confirmed": False},
    )
    monkeypatch.setattr("crypto_screening.run_screening.compute_oi_change_pct", lambda conn, symbol, window_days: None)
    monkeypatch.setattr("crypto_screening.run_screening.compute_volume_spike_pct", lambda conn, symbol: None)

    critical_bybit = {
        **BYBIT_SNAPSHOT,
        "BTCUSDT": {**BYBIT_SNAPSHOT["BTCUSDT"], "funding_rate": Decimal("-0.05"), "price_change_percent": Decimal("83.0")},
    }
    monkeypatch.setattr("crypto_screening.run_screening.fetch_bybit_snapshot", lambda session: critical_bybit)

    results = run_screening()

    assert [s.symbol for s in results["watch"]] == ["BTCUSDT"]
    assert results["short"] == []
    assert results["long"] == []
