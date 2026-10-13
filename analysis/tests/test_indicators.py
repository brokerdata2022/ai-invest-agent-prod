"""
Тести crypto_screening/indicators.py — чисті функції, відомі числові
приклади (той самий стиль, що test_trend.py).
"""

import pytest

from crypto_screening.indicators import rsi


def test_rsi_returns_none_when_not_enough_history():
    assert rsi([1.0, 2.0, 3.0], period=14) is None


def test_rsi_all_gains_is_100():
    values = [float(v) for v in range(1, 20)]  # монотонне зростання
    assert rsi(values, period=14) == 100.0


def test_rsi_all_losses_is_0():
    values = [float(v) for v in range(20, 1, -1)]  # монотонне падіння
    assert rsi(values, period=14) == 0.0


def test_rsi_flat_series_is_neutral_50():
    values = [10.0] * 20  # жодного руху
    assert rsi(values, period=14) == 50.0


def test_rsi_matches_hand_computed_wilder_smoothing():
    # period=2, вручну прораховано за формулою згладжування Вайлдера:
    # values=[10,12,11,13] -> deltas=[2,-1,2] -> gains=[2,0,2], losses=[0,1,0]
    # seed: avg_gain=(2+0)/2=1.0, avg_loss=(0+1)/2=0.5
    # i=2: avg_gain=(1.0*1+2)/2=1.5, avg_loss=(0.5*1+0)/2=0.25
    # RS=1.5/0.25=6.0 -> RSI=100-100/7=85.714285...
    values = [10.0, 12.0, 11.0, 13.0]
    assert rsi(values, period=2) == pytest.approx(85.714285714, abs=1e-6)
