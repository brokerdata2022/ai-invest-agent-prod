import pytest

from expectations.comparison_methods import (
    COMPARISON_METHOD,
    REQUIRED_OBSERVATIONS,
    compute_actual,
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
