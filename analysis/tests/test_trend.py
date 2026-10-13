"""
Тести trend.py — чисті функції, відомі числові приклади (без БД/мережі).
"""

from forecasting.trend import linear_trend_forecast, naive_forecast


def test_naive_forecast_returns_last_value():
    assert naive_forecast([1.0, 2.0, 3.0]) == 3.0


def test_naive_forecast_empty_returns_none():
    assert naive_forecast([]) is None


def test_linear_trend_forecast_perfect_line():
    # y = x -> наступна точка (x=4) має дати 4.0
    assert linear_trend_forecast([0.0, 1.0, 2.0, 3.0]) == 4.0


def test_linear_trend_forecast_extrapolates_multiple_periods():
    assert linear_trend_forecast([0.0, 1.0, 2.0, 3.0], periods_ahead=3) == 6.0


def test_linear_trend_forecast_constant_series():
    # без тренду -> прогноз = те саме значення
    assert linear_trend_forecast([5.0, 5.0, 5.0]) == 5.0


def test_linear_trend_forecast_needs_at_least_two_points():
    assert linear_trend_forecast([1.0]) is None
    assert linear_trend_forecast([]) is None


# ============================================================
# Локальне вікно базової лінії
# ============================================================
# Живий збій 2026-10-06: історію показників добрали з 14-30 точок до
# ~285, і регресія через УСЮ історію почала будувати пряму через
# 20+ років — MAE 28462 проти 5064 у naive на `retail_sales`. Базова
# лінія, що стала абсурдною, не виконує своєї ролі: з нею неможливо
# сказати "LLM не кращий за просту формулу".


def test_window_limits_regression_to_recent_points():
    """Довга історія з перегином: без вікна пряма тягнеться від самого
    початку, з вікном — лише по свіжій ділянці."""
    values = [float(v) for v in range(100)] + [200.0, 210.0, 220.0, 230.0]

    full = linear_trend_forecast(values)
    windowed = linear_trend_forecast(values, window=4)

    assert full is not None and windowed is not None
    # Свіжа ділянка йде кроком +10 -> наступне ~240.
    assert 235 < windowed < 245
    # Повна історія дає зовсім інший (і тут безглуздий) результат.
    assert abs(full - windowed) > 50


def test_window_ignored_when_history_is_shorter():
    values = [10.0, 12.0, 14.0]
    assert linear_trend_forecast(values, window=50) == linear_trend_forecast(values)


def test_window_below_two_points_is_ignored():
    """Регресія потребує 2+ точок — вікно 1 не має сенсу й не має
    ламати розрахунок."""
    values = [10.0, 12.0, 14.0, 16.0]
    assert linear_trend_forecast(values, window=1) == linear_trend_forecast(values)
