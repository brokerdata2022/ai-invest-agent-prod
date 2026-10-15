import pytest

from expectations.comparison_methods import (
    CLOSER,
    COMPARISON_METHOD,
    FARTHER,
    REQUIRED_OBSERVATIONS,
    TIE,
    compare_to_market,
    compute_actual,
    forecast_in_comparison_units,
)


def _obs(*values):
    """Найновіше перше — той самий порядок, що common/db.py:fetch_recent."""
    return [{"value": v} for v in values]


def test_level_direct_uses_latest_value():
    assert compute_actual("level_direct", _obs(4.1)) == 4.1


def test_diff_level_subtracts_previous_from_latest():
    assert compute_actual("diff_level", _obs(158_180, 158_000)) == pytest.approx(180)


def test_mom_pct_computes_percent_change():
    # 314.5 -> 315.75, +0.397%
    result = compute_actual("mom_pct", _obs(315.75, 314.5))
    assert result == pytest.approx(0.397, abs=0.001)


def test_qoq_pct_annualized():
    # 2% q/q non-annualized -> ((1.02)^4 - 1) * 100 ~= 8.24%
    result = compute_actual("qoq_pct_annualized", _obs(102.0, 100.0))
    assert result == pytest.approx(8.24, abs=0.01)


def test_yoy_pct_compares_latest_to_thirteenth_point():
    observations = _obs(105.0, *[100.0] * 11, 100.0)
    result = compute_actual("yoy_pct", observations)
    assert result == pytest.approx(5.0)


def test_returns_none_when_not_enough_observations():
    assert compute_actual("mom_pct", _obs(100.0)) is None


def test_mom_pct_returns_none_on_zero_previous():
    assert compute_actual("mom_pct", _obs(10.0, 0.0)) is None


def test_unknown_method_raises():
    with pytest.raises(ValueError):
        compute_actual("not_a_method", _obs(1.0))


def test_every_registered_metric_has_a_known_required_observation_count():
    for metric_id, method in COMPARISON_METHOD.items():
        assert method in REQUIRED_OBSERVATIONS, f"{metric_id}: невідомий метод {method!r}"


# ============================================================
# ТРИ ЧИСЛА ПОРУЧ (2026-10-15)
# ============================================================


def test_forecast_converted_by_the_same_method_as_actual():
    """Прогноз лежить РІВНЕМ, порівняння — у m/m %. Переведення має
    робити той самий compute_actual, інакше "агент проти ринку"
    порівнювало б різні величини."""
    observations = _obs(336.298, 334.131)  # реліз і попередній місяць
    actual = compute_actual("mom_pct", observations)
    forecast = forecast_in_comparison_units("mom_pct", 334.9, observations)

    assert actual == pytest.approx(0.6485, abs=0.0001)
    # 334.9 проти серпневих 334.131 -> +0.23%
    assert forecast == pytest.approx(0.2301, abs=0.0001)


def test_forecast_substitutes_latest_not_appends():
    """Прогноз стає НАЙНОВІШИМ значенням, історія лишається та сама —
    інакше вікно зсунулось би й m/m рахувалось би не від того місяця."""
    observations = _obs(100.0, 50.0)
    assert forecast_in_comparison_units("mom_pct", 75.0, observations) == pytest.approx(50.0)


def test_forecast_level_direct_passes_through():
    assert forecast_in_comparison_units("level_direct", 4.3, _obs(4.1)) == 4.3


def test_forecast_none_without_observations():
    assert forecast_in_comparison_units("mom_pct", 100.0, []) is None


def test_market_closer_when_its_error_smaller():
    assert compare_to_market(actual=0.6485, forecast=0.2301, expected=0.6) == FARTHER


def test_agent_closer_when_its_error_smaller():
    assert compare_to_market(actual=0.65, forecast=0.64, expected=0.40) == CLOSER


def test_tie_on_equal_errors():
    assert compare_to_market(actual=1.0, forecast=0.9, expected=1.1) == TIE


def test_no_verdict_without_forecast():
    """Відсутність прогнозу — не поразка агента, а відсутність
    твердження (той самий принцип, що no_claim у журналі наслідків)."""
    assert compare_to_market(actual=1.0, forecast=None, expected=1.1) is None
