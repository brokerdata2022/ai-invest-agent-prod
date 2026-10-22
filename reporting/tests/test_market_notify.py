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


# --- сесійний бриф (2026-10-22, пункт 5 плану сесії) ---

from market_notify import format_trading_block  # noqa: E402

_SYNTH = {
    "cluster_count": 2, "direction": "up", "confidence": Decimal("0.6"),
    "summary": "Risk-on перед відкриттям.", "source_refs": [], "session": "us",
}


def test_brief_lists_session_assets_with_why_and_levels():
    trading = [{
        "id": 1, "asset_id": "skyd", "label": "Skydance (SKYD)", "kind": "stock",
        "direction": "up", "why": "звіт SKYD 22.10; тренд: +6% коротке",
        "levels": '{"last": "7.87", "high": "8.40", "low": "6.61", "days": 20, '
                  '"to_high_pct": "+6.7", "to_low_pct": "-16.0", "daily_move_pct": "3.10"}',
    }]
    text = format_message(_SYNTH, trading)
    assert "Перед відкриттям Нью-Йорка" in text
    assert "Активи для торгів у цю сесію — 1" in text
    assert "Skydance (SKYD)" in text and "вгору" in text
    assert "звіт SKYD" in text
    assert "макс 20 дн 8.40" in text and "±3.10%" in text
    assert "ціна 7.87" in text


def test_brief_says_plainly_when_nothing_to_trade():
    """"Якщо немає — прямо сказати" (пункт 5 плану)."""
    text = format_message(_SYNTH, [])
    assert "Активи для торгів у цю сесію: немає" in text


def test_daily_synthesis_without_session_has_no_trading_block():
    assert format_trading_block(None) == []


def test_brief_without_synthesis_still_lists_trading_assets():
    """Синтезу не склалось (немає підтверджених історій) — активи для
    торгів перед відкриттям однаково потрібні."""
    from market_notify import format_brief_without_synthesis

    text = format_brief_without_synthesis("us", [])
    assert "Перед відкриттям Нью-Йорка" in text
    assert "синтезу стану ринку цього разу немає" in text
    assert "Активи для торгів у цю сесію: немає" in text
