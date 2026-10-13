"""
Базові трендові моделі прогнозування одного показника (docs/archive/plan-journal-2026-10-11.md, Фаза 2:
"почати з простого — трендові моделі — перш ніж ускладнювати").

Чисті функції — жодного SQL/API тут (rule 1, CLAUDE.md), тестуються на
голих списках чисел. Приймають значення в ХРОНОЛОГІЧНОМУ порядку
(найстаріше перше) — на відміну від common/db.py:fetch_recent() чи
expectations/comparison_methods.py (там найновіше перше): тут
природніше для регресії/екстраполяції вперед у часі. Виклик
(backtest.py/forecast_metric.py) відповідає за розворот.
"""

from typing import Optional


def naive_forecast(values: list[float]) -> Optional[float]:
    """Найпростіший можливий прогноз — останнє відоме значення без змін.
    Потрібен як базова лінія для backtest.py: трендова модель має сенс
    лише тоді, коли вона стабільно точніша за це (analysis/CLAUDE.md:
    кожна модель прогнозування повинна бути backtestable, "без цього
    неможливо зрозуміти, чи модель взагалі має сенс")."""
    if not values:
        return None
    return values[-1]


def linear_trend_forecast(
    values: list[float], periods_ahead: int = 1, window: Optional[int] = None
) -> Optional[float]:
    """Звичайна лінійна регресія (метод найменших квадратів) значення
    від порядкового номера точки (0, 1, 2, ...), екстраполяція на
    `periods_ahead` кроків уперед. Свідомо не враховує фактичні дати
    (той самий стиль задокументованого спрощення, що
    expectations/comparison_methods.py:yoy_pct) — припускає регулярний
    інтервал між точками. Потрібно щонайменше 2 точки.

    `window` (2026-10-06) — рахувати лише по останніх N точках.
    Без нього регресія через усю історію (після backfill це ~285
    точок, тобто 20+ років) будує пряму, що не має нічого спільного з
    наступним місяцем: живо MAE 28462 проти 5064 у naive. Базова
    лінія мусить бути ЛОКАЛЬНОЮ, інакше вона не базова лінія, а
    карикатура."""
    if window is not None and window >= 2:
        values = values[-window:]
    n = len(values)
    if n < 2:
        return None

    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n

    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None

    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values))
    slope = numerator / denominator
    intercept = mean_y - slope * mean_x

    next_x = (n - 1) + periods_ahead
    return intercept + slope * next_x
