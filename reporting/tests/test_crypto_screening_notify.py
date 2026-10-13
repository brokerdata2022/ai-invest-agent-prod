from datetime import datetime, timezone
from decimal import Decimal

from crypto_screening_notify import format_message


def _row(symbol, status="watch", last_pump_pct="45.2", last_funding_rate="-0.025", reason="памп тримається"):
    return {
        "symbol": symbol,
        "status": status,
        "pump_pct_at_detection": Decimal("30.0"),
        "last_pump_pct": Decimal(last_pump_pct) if last_pump_pct is not None else None,
        "last_funding_rate": Decimal(last_funding_rate) if last_funding_rate is not None else None,
        "reason": reason,
        "detected_at": datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
    }


def test_format_message_empty():
    assert "немає" in format_message([])


def test_format_message_known_rows():
    rows = [_row("QNTUSDT", status="watch"), _row("SOONUSDT", status="short")]
    text = format_message(rows)

    assert "QNTUSDT" in text
    assert "SOONUSDT" in text
    assert "WATCH" in text
    assert "SHORT" in text
    assert "+45.2%" in text
    assert "-2.500%" in text
    # Тон: жодних прямих інструкцій купити/продати/шорти
    # (analysis/CLAUDE.md "Заборонені формулювання").
    assert "продай" not in text.lower() and "купи" not in text.lower()


def test_format_message_falls_back_to_detection_pump_when_no_last_pump():
    row = _row("ACMEUSDT", last_pump_pct=None)
    text = format_message([row])
    assert "+30.0%" in text


def test_format_message_omits_missing_funding():
    row = _row("ACMEUSDT", last_funding_rate=None)
    text = format_message([row])
    assert "funding" not in text
