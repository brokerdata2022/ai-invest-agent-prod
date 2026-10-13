from datetime import datetime, timezone
from decimal import Decimal

from crypto_long_notify import format_message


def _row(symbol, oi_change_pct="12.5", rsi_value="62", funding_rate="0.0001"):
    return {
        "symbol": symbol,
        "oi_change_pct": Decimal(oi_change_pct) if oi_change_pct is not None else None,
        "rsi_value": Decimal(rsi_value) if rsi_value is not None else None,
        "funding_rate": Decimal(funding_rate) if funding_rate is not None else None,
        "run_at": datetime(2026, 10, 2, 6, 25, tzinfo=timezone.utc),
    }


def test_format_message_empty():
    assert "немає" in format_message([])


def test_format_message_known_rows():
    rows = [_row("BTCUSDT"), _row("ETHUSDT", oi_change_pct="8.0", rsi_value="55")]
    text = format_message(rows)

    assert "BTCUSDT" in text
    assert "ETHUSDT" in text
    assert "+12.5%" in text
    assert "RSI 62" in text
    assert "2 символів" in text
    # Тон: ніякої прямої інструкції купити/входити в лонг
    # (analysis/CLAUDE.md "Заборонені формулювання").
    assert "купуй" not in text.lower() and "входь" not in text.lower()


def test_format_message_missing_metrics_show_na():
    rows = [_row("ACMEUSDT", oi_change_pct=None, rsi_value=None)]
    text = format_message(rows)
    assert "н/д" in text


def test_format_message_respects_limit_but_keeps_total_count():
    rows = [_row(f"SYM{i}USDT") for i in range(35)]
    text = format_message(rows, limit=30)

    assert "35 символів" in text
    assert "топ 30" in text
    assert "SYM29USDT" in text
    assert "SYM30USDT" not in text
