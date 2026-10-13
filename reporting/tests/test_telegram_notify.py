from datetime import date, datetime, timezone
from decimal import Decimal

from telegram_notify import format_message


def test_format_message_known_metric():
    row = {
        "source": "fred",
        "metric_id": "cpi",
        "value": Decimal("314.121"),
        "observed_at": date(2026, 7, 1),
        "fetched_at": datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc),
        "revision": 1,
    }
    text = format_message(row, "cpi")
    assert "CPI (інфляція, США)" in text
    assert "314.121" in text
    assert "2026-07-01" in text
    assert "fred" in text


def test_format_message_unknown_metric_falls_back_to_id():
    row = {
        "source": "fred",
        "metric_id": "some_new_metric",
        "value": Decimal("1.0"),
        "observed_at": date(2026, 1, 1),
        "fetched_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "revision": 1,
    }
    text = format_message(row, "some_new_metric")
    assert "some_new_metric" in text
