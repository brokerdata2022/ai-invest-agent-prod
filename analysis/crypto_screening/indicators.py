"""
Технічні індикатори для крипто-скринінгу лонг/шорт/спостереження
(крок 3 з 5, docs/decisions.md 2026-09-27) — той самий стиль, що
`forecasting/trend.py`: чисті функції, жодного SQL/API тут (rule 1,
CLAUDE.md), тестуються на голих списках чисел. Приймають значення в
ХРОНОЛОГІЧНОМУ порядку (найстаріше перше) — той самий порядок, що
`trend.py` (виклик відповідає за розворот `fetch_recent()`-результату).

RSI тут — вхідний ІНДИКАТОР для скринінг-логіки (крок 4), не прогнозна
модель сама по собі — на відміну від `forecasting/trend.py`
(`linear_trend_forecast`), backtest (analysis/CLAUDE.md, "чи модель
взагалі має сенс") до нього не застосовний: RSI не прогнозує наступне
значення, лише описує поточний імпульс. Тренд/backtest для LONG-сетапу
(докладніше docs/decisions.md) перевикористовує вже готовий
`forecasting/trend.py`, не дублюється тут.
"""

from typing import Optional


def rsi(values: list[float], period: int = 14) -> Optional[float]:
    """Стандартний RSI зі згладжуванням Вайлдера (Wilder's smoothing) —
    найпоширеніший варіант розрахунку. Повертає RSI лише для ОСТАННЬОЇ
    точки серії (не всю історію) — саме це потрібно скринінгу (крок 4):
    поточний стан імпульсу монети, не ряд значень. Потребує щонайменше
    `period + 1` точок.

    Крайні випадки:
    - Немає жодного руху (усі дельти = 0) -> 50.0 (нейтрально, не 100 —
      100 означало б "лише зростання", що для повністю пласкої серії
      неправда).
    - Лише зростання (жодного падіння) -> 100.0 (граничний перегрів).
    - Лише падіння (жодного зростання) -> 0.0 (граничне виснаження).
    """
    n = len(values)
    if n < period + 1:
        return None

    deltas = [values[i] - values[i - 1] for i in range(1, n)]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))
