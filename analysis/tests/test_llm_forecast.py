"""
Тести чистих функцій analysis/forecasting/llm_forecast.py — промпт,
розбір відповіді LLM і гейт правдоподібності. Без мережі/БД (той самий
підхід, що test_trend.py/test_synthesize_calendar_outlook.py).
"""

import json

import pytest

import config
from llm_common import SynthesisResponseError

from forecasting.llm_forecast import (
    mean_delta,
    PLAUSIBILITY_FACTOR,
    build_prompt,
    describe_metric,
    is_plausible,
    parse_forecast_response,
)

HISTORY = [
    {"observed_at": "2026-04-30", "value": 320.0},
    {"observed_at": "2026-05-31", "value": 320.8},
    {"observed_at": "2026-06-30", "value": 321.3},
    {"observed_at": "2026-07-31", "value": 321.9},
    {"observed_at": "2026-08-31", "value": 322.4},
]

VALID_RESPONSE = {
    "forecast_value": 323.0,
    "direction": "up",
    "confidence": 0.7,
    "summary": "Очікується 323.0 — тренд зростання зберігається.",
    "reasoning": "Останні 5 місяців крок +0.5..+0.8 без розворотів.",
}


# --- build_prompt ---------------------------------------------------


def test_build_prompt_includes_every_history_point():
    prompt = build_prompt("cpi", describe_metric("cpi"), HISTORY)
    for obs in HISTORY:
        assert str(obs["observed_at"]) in prompt
    assert "320.8" in prompt
    assert "322.4" in prompt


def test_build_prompt_includes_metric_description_with_units():
    prompt = build_prompt("cpi", describe_metric("cpi"), HISTORY)
    assert "metric_id=cpi" in prompt
    # Опис несе одиниці — саме те, без чого LLM плутає рівень індексу
    # з річною зміною (llm_forecast.py, розділ про одиниці).
    assert "РІВЕНЬ індексу" in prompt


def test_build_prompt_reports_history_size():
    prompt = build_prompt("cpi", "CPI", HISTORY)
    assert f"Кількість точок історії: {len(HISTORY)}" in prompt


def test_describe_metric_falls_back_to_metric_id():
    assert describe_metric("made_up_metric") == "made_up_metric"


# --- parse_forecast_response ----------------------------------------


def test_parse_forecast_response_valid():
    result = parse_forecast_response(json.dumps(VALID_RESPONSE))
    assert result.forecast_value == pytest.approx(323.0)
    assert result.direction == "up"
    assert result.confidence == pytest.approx(0.7)
    assert result.summary.startswith("Очікується")


def test_parse_forecast_response_accepts_numeric_string():
    """LLM часто віддає число рядком — це не причина викидати прогноз."""
    result = parse_forecast_response(json.dumps(dict(VALID_RESPONSE, forecast_value="323.0")))
    assert result.forecast_value == pytest.approx(323.0)


def test_parse_forecast_response_rejects_non_numeric_forecast():
    with pytest.raises(SynthesisResponseError, match="forecast_value"):
        parse_forecast_response(json.dumps(dict(VALID_RESPONSE, forecast_value="близько 323")))


def test_parse_forecast_response_rejects_missing_required_field():
    payload = dict(VALID_RESPONSE)
    del payload["summary"]
    with pytest.raises(SynthesisResponseError, match="бракує полів"):
        parse_forecast_response(json.dumps(payload))


def test_parse_forecast_response_accepts_missing_reasoning():
    """Живий кейс `unemployment_rate` (2026-10-04): DeepSeek віддав
    коректний прогноз без `reasoning` — і вся точка backtest гинула.
    `reasoning` потрібне для читабельності аудиту, а повна сира
    відповідь однаково лежить у llm_call_log, тож прогноз приймається,
    обґрунтування підставляється з summary."""
    payload = dict(VALID_RESPONSE)
    del payload["reasoning"]
    result = parse_forecast_response(json.dumps(payload))
    assert result.forecast_value == pytest.approx(323.0)
    assert result.reasoning == VALID_RESPONSE["summary"]


def test_parse_forecast_response_treats_empty_reasoning_as_missing():
    result = parse_forecast_response(json.dumps(dict(VALID_RESPONSE, reasoning="")))
    assert result.reasoning == VALID_RESPONSE["summary"]


def test_parse_forecast_response_rejects_unknown_direction():
    with pytest.raises(SynthesisResponseError, match="direction"):
        parse_forecast_response(json.dumps(dict(VALID_RESPONSE, direction="вгору")))


def test_parse_forecast_response_rejects_confidence_out_of_range():
    with pytest.raises(SynthesisResponseError, match="confidence"):
        parse_forecast_response(json.dumps(dict(VALID_RESPONSE, confidence=1.5)))


def test_parse_forecast_response_rejects_non_json():
    with pytest.raises(SynthesisResponseError):
        parse_forecast_response("прогноз: 323")


# --- is_plausible ---------------------------------------------------

VALUES = [float(o["value"]) for o in HISTORY]


def test_is_plausible_accepts_forecast_near_last_value():
    plausible, reason = is_plausible(VALUES, 323.0)
    assert plausible
    assert reason is None


def test_is_plausible_rejects_unit_confusion():
    """Живий ризик: LLM віддає річну зміну (3.2%) замість рівня індексу
    (322.4) — саме те, що гейт має ловити."""
    plausible, reason = is_plausible(VALUES, 3.2)
    assert not plausible
    assert "одиниць" in reason


def _max_step() -> float:
    return max(abs(VALUES[i] - VALUES[i - 1]) for i in range(1, len(VALUES)))


def test_is_plausible_accepts_forecast_just_inside_limit():
    # Свідомо 0.99× межі, не рівно межа: порівняння з точною межею
    # залежало б від останніх бітів float-арифметики, а не від логіки.
    inside = VALUES[-1] + PLAUSIBILITY_FACTOR * _max_step() * 0.99
    plausible, _ = is_plausible(VALUES, inside)
    assert plausible


def test_is_plausible_rejects_just_beyond_limit():
    beyond = VALUES[-1] + PLAUSIBILITY_FACTOR * _max_step() * 1.01
    plausible, reason = is_plausible(VALUES, beyond)
    assert not plausible
    assert reason


def test_is_plausible_skips_check_on_short_history():
    """Замало точок — гейт не має на чому працювати, тож пропускає
    (краще зберегти прогноз, ніж відкинути за відсутності міри)."""
    plausible, reason = is_plausible([100.0, 101.0], 999.0)
    assert plausible
    assert reason is None


def test_is_plausible_skips_check_on_flat_series():
    """Вироджена історія (жодного руху): інакше гейт пропускав би лише
    точний повтор останнього значення."""
    plausible, _ = is_plausible([2.0, 2.0, 2.0, 2.0, 2.0], 2.5)
    assert plausible


# ============================================================
# Кроки (Δ) у промпті — P1 плану покращення прогнозу
# ============================================================
# Причина (видна в усіх прогонах бектесту): обидві сталі слабкості
# прогнозу — РОЗВОРОТИ (ряд почав падати, модель продовжувала
# зростання) і УСКОРЕННЯ (крок прискорився, модель екстраполювала
# попередній) — мають одне джерело: модель бачила лише РІВНІ й мусила
# рахувати різниці сама.


def test_mean_delta_averages_recent_steps():
    values = [100.0, 101.0, 103.0, 106.0]  # кроки +1, +2, +3
    assert mean_delta(values, 3) == pytest.approx(2.0)
    assert mean_delta(values, 1) == pytest.approx(3.0)


def test_mean_delta_none_when_not_enough_steps():
    """Вікно 5 кроків потребує 6 точок — менше це "невідомо", не нуль."""
    assert mean_delta([100.0, 101.0], 5) is None


def test_mean_delta_handles_decline():
    values = [10.0, 9.0, 7.0]
    assert mean_delta(values, 2) == pytest.approx(-1.5)


def test_prompt_shows_delta_per_row():
    rows = [
        {"observed_at": "2026-01-01", "value": 100},
        {"observed_at": "2026-02-01", "value": 101.5},
        {"observed_at": "2026-03-01", "value": 100.3},
    ]
    prompt = build_prompt("cpi", "CPI", rows, include_deltas=True)

    assert "Δ +1.5" in prompt
    # Розворот мусить бути видимим ЗНАКОМ, не вирахуваним із рівнів.
    assert "Δ -1.2" in prompt


def test_prompt_has_no_delta_for_the_first_point():
    """У першої точки немає "попередньої", тож і Δ бути не може —
    інакше модель прочитала б вигаданий крок."""
    rows = [{"observed_at": "2026-01-01", "value": 100}]
    prompt = build_prompt("cpi", "CPI", rows, include_deltas=True)
    assert "(Δ" not in prompt


def test_prompt_summarises_mean_step_per_window():
    # Найширше вікно (12 кроків) потребує 13 точок — беремо з запасом.
    rows = [
        {"observed_at": f"2026-{i % 12 + 1:02d}-01", "value": 100 + i * 2}
        for i in range(16)
    ]
    prompt = build_prompt("cpi", "CPI", rows, include_deltas=True)

    assert "Середній крок" in prompt
    for window in config.FORECAST_DELTA_WINDOWS:
        assert f"за {window} кроків" in prompt


def test_delta_windows_fit_into_the_history_window():
    """Вікно Δ, більше за історію, яку бачить модель, рахувалось би по
    даних, яких у промпті немає."""
    assert max(config.FORECAST_DELTA_WINDOWS) <= config.FORECAST_HISTORY_POINTS


def test_production_prompt_has_no_deltas_by_default():
    """A/B 2026-10-06: Δ зашкодив 6 показникам із 8 (у середньому
    -2.9 п.п.), тож продакшен лишається на чистих рівнях. Цей тест
    стереже саме РІШЕННЯ: повернути Δ можна лише з новим виміром."""
    rows = [
        {"observed_at": "2026-01-01", "value": 100},
        {"observed_at": "2026-02-01", "value": 102},
    ]
    prompt = build_prompt("cpi", "CPI", rows)
    assert "(Δ" not in prompt
    assert "Середній крок" not in prompt
