"""
LONG-скринінг — окремий алгоритм від SHORT/WATCH (пряма вимога
користувача, 2026-09-27: "зроби окремі алгоритми для фільтрації монет
які претендують на лонг і окремий на шорт") — продовження тренду, не
розворот (докладніше методологія — docs/decisions.md, 2026-09-27).

Чиста функція рішення (rule 1, CLAUDE.md) — приймає вже пораховані
індикатори, не рахує їх сама і не звертається до БД/API:
- `trend_confirmed` — forecasting/backtest.py (MAE тренду < MAE naive
  на історії символу, той самий критерій, що analysis/CLAUDE.md: "чи
  модель взагалі має сенс", не інтуїція).
- `oi_change_pct` — зміна Open Interest за вікно (aggregate_sources.py
  + історія raw_observations).
- `rsi_value` — crypto_screening/indicators.py:rsi().
- `funding_rate` — aggregate_sources.py (`funding_rate_avg`).

Потребує Tier A-допуску (tier_a.py) ЗАРАНІШЕ — тут лише напрямок і
якість сигналу для вже допущених символів.

**Заборонені формулювання (analysis/CLAUDE.md, без винятку):**
результат — лише "бичачий нахил" (direction="up"), НІКОЛИ "купуй"/
"входь у лонг". `reasons` — для аудиту (чому пройшло/не пройшло), не
для показу користувачу як інструкція.

Пороги — значення й пояснення кожного: `config.py`
(єдиний файл для ручного редагування, 2026-10-03).
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from config import MAX_FUNDING_RATE_LONG, RSI_HEALTHY_MAX, RSI_HEALTHY_MIN


@dataclass
class LongSignal:
    symbol: str
    qualifies: bool
    direction: str  # "up" | "none" — НІКОЛИ інструкція купити
    reasons: list[str] = field(default_factory=list)
    # Контекст для персистування/звіту (run_screening.py:
    # crypto_screening/_long_db.py, 2026-10-02) — screen_long() сам їх
    # НЕ заповнює (чиста функція рішення, вхідні метрики вже передані
    # окремими аргументами вище); caller проставляє на вже готовому
    # сигналі, щоб не дублювати значення в сигнатурі функції.
    oi_change_pct: Optional[float] = None
    rsi_value: Optional[float] = None
    funding_rate: Optional[Decimal] = None


def screen_long(
    symbol: str,
    trend_confirmed: bool,
    oi_change_pct: Optional[float],
    rsi_value: Optional[float],
    funding_rate: Optional[Decimal],
) -> LongSignal:
    reasons = []

    if not trend_confirmed:
        reasons.append("висхідний тренд не підтверджений backtest")
    if oi_change_pct is None or oi_change_pct <= 0:
        reasons.append("Open Interest не росте разом із ціною")
    if rsi_value is None or not (RSI_HEALTHY_MIN <= rsi_value <= RSI_HEALTHY_MAX):
        reasons.append(f"RSI поза здоровою зоною [{RSI_HEALTHY_MIN}, {RSI_HEALTHY_MAX}]")
    if funding_rate is not None and funding_rate > MAX_FUNDING_RATE_LONG:
        reasons.append("funding rate аномально високий — перегрітий/дорогий лонг")

    qualifies = not reasons
    return LongSignal(
        symbol=symbol,
        qualifies=qualifies,
        direction="up" if qualifies else "none",
        reasons=reasons,
    )
