"""
Тести common/manual_observation.py — монкіпатч insert_observation
(не реальна БД, той самий принцип, що test_watchlist_db.py).
"""

from datetime import date
from decimal import Decimal

import pytest

import common.manual_observation as manual_observation


def test_rejects_fewer_than_two_source_refs():
    with pytest.raises(ValueError):
        manual_observation.insert_crosschecked_observation(
            conn=None, metric_id="coffee", value=Decimal("288.75"),
            observed_at=date(2026, 10, 2), source_refs=["https://a.example"],
        )


def test_rejects_zero_source_refs():
    with pytest.raises(ValueError):
        manual_observation.insert_crosschecked_observation(
            conn=None, metric_id="coffee", value=Decimal("288.75"),
            observed_at=date(2026, 10, 2), source_refs=[],
        )


def test_builds_record_with_web_crosscheck_source(monkeypatch):
    captured = {}

    def fake_insert_observation(conn, record):
        captured["record"] = record
        return 1

    monkeypatch.setattr(manual_observation, "insert_observation", fake_insert_observation)

    refs = ["https://tradingeconomics.com/commodity/coffee", "https://www.investing.com/commodities/us-coffee-c"]
    result = manual_observation.insert_crosschecked_observation(
        conn=object(), metric_id="coffee", value=Decimal("288.75"),
        observed_at=date(2026, 10, 2), source_refs=refs,
    )

    assert result == 1
    record = captured["record"]
    assert record.source == "web_crosscheck"
    assert record.metric_id == "coffee"
    assert record.value == Decimal("288.75")
    assert record.observed_at == date(2026, 10, 2)
    assert record.raw_payload == {"source_refs": refs}
    assert record.revision is None  # визначає common/db.py, не ця функція
