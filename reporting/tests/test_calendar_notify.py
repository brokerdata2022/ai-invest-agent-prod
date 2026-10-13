from calendar_notify import format_outlook_message

ROW_WEEK = {
    "outlook_date": "2026-10-05", "scope": "week",
    "direction": "up", "confidence": 0.6,
    "summary": "CPI і NFP цього тижня здатні рухати ринок.",
}

ROW_DAY = {
    "outlook_date": "2026-10-06", "scope": "day",
    "direction": "neutral", "confidence": 1.0,
    "summary": "Запланованих релізів немає — спокійний день.",
}

RELEASE_ROWS = [
    {"source": "fred", "metric_id": "cpi", "scheduled_at": "2026-10-06 12:30", "impact_level": "high"},
    {"source": "fred", "metric_id": "unknown_metric", "scheduled_at": "2026-10-07 10:00", "impact_level": None},
]


def test_format_outlook_message_week_header():
    text = format_outlook_message(ROW_WEEK, RELEASE_ROWS)
    assert "📅 Огляд тижня — 2026-10-05" in text


def test_format_outlook_message_day_header():
    text = format_outlook_message(ROW_DAY, [])
    assert "📅 Огляд дня — 2026-10-06" in text
    assert "Запланованих релізів немає." in text


def test_format_outlook_message_lists_events_with_label_and_impact_emoji():
    text = format_outlook_message(ROW_WEEK, RELEASE_ROWS)
    assert "🔴 2026-10-06 12:30 — CPI (інфляція, США)" in text
    assert "❔ 2026-10-07 10:00 — unknown_metric" in text


def test_format_outlook_message_ends_with_direction_and_summary():
    text = format_outlook_message(ROW_WEEK, RELEASE_ROWS)
    assert "🟢 CPI і NFP цього тижня здатні рухати ринок." in text
