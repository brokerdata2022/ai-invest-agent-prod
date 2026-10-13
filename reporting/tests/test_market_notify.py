from decimal import Decimal

from market_notify import format_message


def test_format_message_none_row():
    assert "немає" in format_message(None)


def test_format_message_known_row():
    row = {
        "cluster_count": 3,
        "direction": "down",
        "confidence": Decimal("0.70"),
        "summary": "Risk-off: дохідності зростають разом із ведмежим сигналом.",
        "source_refs": [
            {"title": "Fed signals rates could stay higher for longer", "source_count": 5},
            {"title": "Oil slips on demand worries", "source_count": 2},
        ],
    }
    text = format_message(row)
    assert "risk-off" in text
    assert "0.70" in text
    assert "3 найбільш підтверджених" in text
    assert "Fed signals rates could stay higher for longer" in text
    assert "[5 джерел]" in text


def test_format_message_unknown_direction_falls_back_to_raw_value():
    row = {
        "cluster_count": 1,
        "direction": "something_new",
        "confidence": Decimal("0.50"),
        "summary": "Немає чіткого сигналу.",
        "source_refs": [],
    }
    text = format_message(row)
    assert "something_new" in text
