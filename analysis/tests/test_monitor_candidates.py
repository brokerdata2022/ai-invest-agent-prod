"""
Тести monitor_candidates.py:monitor_once() — той самий підхід, що
test_run_screening.py (monkeypatch усіх БД/мережевих функцій, без
реальної БД/мережі).
"""

from decimal import Decimal

from crypto_screening.monitor_candidates import monitor_once

PUMPING_CANDIDATE = {
    "id": 1,
    "symbol": "PUMPUSDT",
    "status": "candidate",
    "last_oi_usd": None,
    "last_price": None,
}

AGGREGATED_STILL_PUMPING = {
    "PUMPUSDT": {
        "funding_rate_avg": Decimal("0.0003"),
        "open_interest_usd": Decimal("9000000"),
        "quote_volume_24h": Decimal("15000000"),
        "per_exchange": {"bybit_futures": {"price_change_percent": Decimal("40.0")}},
    }
}


def _patch_common(monkeypatch, candidates, aggregated, updates):
    monkeypatch.setattr("crypto_screening.monitor_candidates.fetch_active_candidates", lambda conn: candidates)
    monkeypatch.setattr(
        "crypto_screening.monitor_candidates.fetch_binance_snapshot", lambda session: aggregated.get("__binance__", {})
    )
    monkeypatch.setattr(
        "crypto_screening.monitor_candidates.fetch_bybit_snapshot", lambda session: aggregated.get("__bybit__", {})
    )
    monkeypatch.setattr(
        "crypto_screening.monitor_candidates.fetch_okx_snapshot", lambda session: aggregated.get("__okx__", {})
    )
    monkeypatch.setattr(
        "crypto_screening.monitor_candidates.aggregate_snapshots",
        lambda binance, bybit, okx: aggregated["__result__"],
    )

    def fake_update(conn, candidate_id, old_status, status, pump_pct, funding_rate, oi_usd, reason, price=None):
        updates.append({
            "id": candidate_id, "old_status": old_status, "status": status, "reason": reason, "price": price,
        })

    monkeypatch.setattr("crypto_screening.monitor_candidates.update_candidate", fake_update)


def test_monitor_once_returns_zero_counts_when_no_active_candidates(monkeypatch):
    monkeypatch.setattr("crypto_screening.monitor_candidates.fetch_active_candidates", lambda conn: [])

    counts = monitor_once(conn=None, session=None)

    assert counts == {"short": 0, "watch": 0, "candidate": 0, "closed": 0}


def test_monitor_once_closes_candidate_missing_from_market(monkeypatch):
    updates = []
    _patch_common(monkeypatch, [PUMPING_CANDIDATE], {"__result__": {}}, updates)

    counts = monitor_once(conn=None, session=None)

    assert counts["closed"] == 1
    assert updates[0]["status"] == "closed"
    assert "зник" in updates[0]["reason"]


def test_monitor_once_closes_candidate_when_pump_faded(monkeypatch):
    updates = []
    faded = {
        "PUMPUSDT": {
            "funding_rate_avg": Decimal("0.0001"),
            "open_interest_usd": Decimal("9000000"),
            "quote_volume_24h": Decimal("15000000"),
            "per_exchange": {"bybit_futures": {"price_change_percent": Decimal("2.0")}},  # памп вичерпався
        }
    }
    _patch_common(monkeypatch, [PUMPING_CANDIDATE], {"__result__": faded}, updates)

    counts = monitor_once(conn=None, session=None)

    assert counts["closed"] == 1
    assert "вичерпався" in updates[0]["reason"]


def test_monitor_once_keeps_candidate_when_no_history_yet(monkeypatch):
    updates = []
    _patch_common(monkeypatch, [PUMPING_CANDIDATE], {"__result__": AGGREGATED_STILL_PUMPING}, updates)
    monkeypatch.setattr(
        "crypto_screening.monitor_candidates.compute_monitoring_indicators",
        lambda session, symbol: {"rsi": 60.0, "volume_spike_pct": None},
    )

    counts = monitor_once(conn=None, session=None)

    # last_oi_usd — None (перший прогін) -> oi_change None -> "none" -> лишається candidate
    assert counts["candidate"] == 1
    assert updates[0]["status"] == "candidate"


def test_monitor_once_promotes_to_short_when_exhaustion_confirmed(monkeypatch):
    updates = []
    candidate_with_history = {**PUMPING_CANDIDATE, "last_oi_usd": Decimal("10000000")}
    aggregated = {
        "PUMPUSDT": {
            "funding_rate_avg": Decimal("0.0003"),
            "open_interest_usd": Decimal("9000000"),   # впало з 10M -> -10%
            "quote_volume_24h": Decimal("15000000"),
            "per_exchange": {"bybit_futures": {"price_change_percent": Decimal("40.0")}},
        }
    }
    _patch_common(monkeypatch, [candidate_with_history], {"__result__": aggregated}, updates)
    monkeypatch.setattr(
        "crypto_screening.monitor_candidates.compute_monitoring_indicators",
        # RSI перегрітий (>=70) + сплеск обсягу (>=20%) на 4г-ТФ (2026-10-03)
        lambda session, symbol: {"rsi": 75.0, "volume_spike_pct": 50.0},
    )

    counts = monitor_once(conn=None, session=None)

    assert counts["short"] == 1
    assert updates[0]["status"] == "short"


def test_monitor_once_promotes_to_short_via_hourly_price_drop_without_volume_spike(monkeypatch):
    # Живий кейс 2026-10-03 (MAGMAUSDT): сплеск обсягу на 4г-ТФ не
    # підтверджує (2%, нижче порогу 20%), але ціна впала різко за
    # останню годину (last_price -> поточний Binance mark_price, -7%)
    # -- цього досить для SHORT без сплеску обсягу
    # (config.py:HOURLY_PRICE_DROP_PCT).
    updates = []
    candidate_with_history = {
        **PUMPING_CANDIDATE,
        "last_oi_usd": Decimal("10000000"),
        "last_price": Decimal("100"),
    }
    aggregated = {
        "PUMPUSDT": {
            "funding_rate_avg": Decimal("0.0003"),
            "open_interest_usd": Decimal("9000000"),   # впало з 10M -> -10%
            "quote_volume_24h": Decimal("15000000"),
            "per_exchange": {
                "bybit_futures": {"price_change_percent": Decimal("40.0")},
                "binance_futures": {"mark_price": Decimal("93")},  # 100 -> 93, -7% за годину
            },
        }
    }
    _patch_common(monkeypatch, [candidate_with_history], {"__result__": aggregated}, updates)
    monkeypatch.setattr(
        "crypto_screening.monitor_candidates.compute_monitoring_indicators",
        lambda session, symbol: {"rsi": 75.0, "volume_spike_pct": 2.0},
    )

    counts = monitor_once(conn=None, session=None)

    assert counts["short"] == 1
    assert "годину" in updates[0]["reason"]
    assert updates[0]["price"] == Decimal("93")  # нова last_price для наступної години


def test_monitor_once_marks_watch_on_critical_funding(monkeypatch):
    updates = []
    critical = {
        "PUMPUSDT": {
            "funding_rate_avg": Decimal("-0.05"),
            "open_interest_usd": Decimal("9000000"),
            "quote_volume_24h": Decimal("15000000"),
            "per_exchange": {"bybit_futures": {"price_change_percent": Decimal("40.0")}},
        }
    }
    _patch_common(monkeypatch, [PUMPING_CANDIDATE], {"__result__": critical}, updates)
    monkeypatch.setattr(
        "crypto_screening.monitor_candidates.compute_monitoring_indicators",
        lambda session, symbol: {"rsi": 60.0, "volume_spike_pct": None},
    )

    counts = monitor_once(conn=None, session=None)

    assert counts["watch"] == 1
    assert updates[0]["status"] == "watch"
