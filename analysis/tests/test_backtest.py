"""
Тести backtest_metric() — на синтетичній історії (без реальної БД).
"""

from forecasting.backtest import backtest_metric


def test_backtest_metric_prefers_trend_on_linear_series():
    # Ідеально лінійний ряд -> тренд має бути практично безпомилковим,
    # значно кращим за naive (яке завжди на крок позаду тренду).
    observations_desc = [
        {"value": float(v), "observed_at": f"2026-{v:02d}-01"}
        for v in range(12, 0, -1)
    ]
    result = backtest_metric("test_metric", observations_desc, min_history=3)

    assert result["n_points"] > 0
    assert result["trend_mae"] < 1e-6
    assert result["trend_mae"] < result["naive_mae"]


def test_backtest_metric_returns_none_maes_when_not_enough_points():
    observations_desc = [
        {"value": 1.0, "observed_at": "2026-01-01"},
        {"value": 2.0, "observed_at": "2026-02-01"},
    ]
    result = backtest_metric("test_metric", observations_desc, min_history=6)

    assert result["n_points"] == 0
    assert result["trend_mae"] is None
    assert result["naive_mae"] is None
