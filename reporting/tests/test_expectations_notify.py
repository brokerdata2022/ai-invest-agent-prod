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


# ============================================================
# ТРИ ЧИСЛА ПОРУЧ (2026-10-15)
# ============================================================

_BASE_ROW = {
    "metric_id": "cpi",
    "observed_at": date(2026, 9, 1),
    "actual_value": 0.6485,
    "expected_value_raw": "0.6%",
    "expected_value_parsed": 0.6,
    "surprise": 0.0485,
    "surprise_pct": 8.1,
    "comparison_method": "mom_pct",
}


def test_shows_agent_forecast_next_to_fact_and_consensus():
    """Доти прогноз агента жив в ОКРЕМОМУ повідомленні, і порівняти
    "ми проти консенсусу" можна було лише SQL-запитом."""
    row = {
        **_BASE_ROW,
        "forecast_value_parsed": 0.2301,
        "forecast_method": "llm",
        "forecast_verdict": "farther_than_market",
    }
    text = format_message([row])

    assert "0.6485" in text          # факт
    assert "0.6" in text             # консенсус
    assert "0.2301" in text          # наш прогноз
    assert "далі за консенсус" in text
    assert "ШІ" in text              # чим зроблено прогноз


def test_says_plainly_when_there_was_no_forecast():
    """Відсутність прогнозу — не поразка агента; писати її треба
    словами, а не пропускати рядок."""
    row = {**_BASE_ROW, "forecast_value_parsed": None,
           "forecast_method": None, "forecast_verdict": None}
    text = format_message([row])

    assert "прогнозу на цей реліз не було" in text


def test_market_expectation_is_called_consensus_not_forecast():
    """Два різні числа не можуть називатись одним словом: нижче стоїть
    НАШ прогноз (живий огляд формату 2026-10-15)."""
    row = {**_BASE_ROW, "forecast_value_parsed": 0.23,
           "forecast_method": "llm", "forecast_verdict": "closer_than_market"}
    text = format_message([row])

    assert "vs консенсус" in text
    assert "(прогноз:" not in text


def test_older_rows_without_forecast_columns_still_render():
    """Рядки, записані до появи стовпців, не мають ламати звіт."""
    text = format_message([dict(_BASE_ROW)])

    assert "CPI" in text
