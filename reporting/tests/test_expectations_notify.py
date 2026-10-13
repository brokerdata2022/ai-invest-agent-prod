from datetime import date

from expectations_notify import format_message


def test_format_message_empty():
    assert "немає" in format_message([])


def test_format_message_known_metric_with_surprise_pct():
    row = {
        "metric_id": "cpi",
        "observed_at": date(2026, 9, 30),
        "actual_value": 0.397,
        "expected_value_raw": "0.4%",
        "expected_value_parsed": 0.4,
        "surprise": -0.003,
        "surprise_pct": -1.25,
        "comparison_method": "mom_pct",
    }
    text = format_message([row])
    assert "CPI (інфляція, США)" in text
    assert "0.397" in text
    assert "0.4%" in text
    assert "-1.2%" in text  # surprise_pct округлюється форматом :+.1f


def test_format_message_includes_synthesis_summary_when_present():
    row = {
        "metric_id": "cpi",
        "observed_at": date(2026, 9, 30),
        "actual_value": 0.397,
        "expected_value_raw": "0.4%",
        "expected_value_parsed": 0.4,
        "surprise": -0.003,
        "surprise_pct": -1.25,
        "comparison_method": "mom_pct",
        "synthesis_summary": "Вийшло трохи нижче за прогноз — незначне послаблення інфляційного тиску.",
    }
    text = format_message([row])
    assert "→ Вийшло трохи нижче за прогноз" in text


def test_format_message_omits_surprise_pct_when_none():
    row = {
        "metric_id": "some_new_metric",
        "observed_at": date(2026, 9, 30),
        "actual_value": 1.0,
        "expected_value_raw": "0",
        "expected_value_parsed": 0.0,
        "surprise": 1.0,
        "surprise_pct": None,
        "comparison_method": "level_direct",
    }
    text = format_message([row])
    assert "some_new_metric" in text
    assert "%)" not in text.split("Сюрприз:")[1]
