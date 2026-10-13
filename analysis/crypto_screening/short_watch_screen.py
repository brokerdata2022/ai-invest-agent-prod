"""
SHORT/WATCH-скринінг — окремий алгоритм від LONG (пряма вимога
користувача, 2026-09-27) — виснаження/розворот ПІСЛЯ пампу, не просто
"ціна падає". Той самий стиль чистої функції рішення, що long_screen.py
— приймає вже пораховані індикатори (aggregate_sources.py, forecasting/,
crypto_screening/indicators.py), не рахує їх сама.

**Критично для правильного розуміння (пряма вимога користувача,
2026-09-27, докладніше docs/decisions.md):** критично негативний
funding ПІСЛЯ пампу — це НЕ підтвердження шорту, а сигнал, що вже
багато шортистів У ПОЗИЦІЇ (натовп), яких може "винести" (short
squeeze) — відкривати НОВИЙ шорт проти вже перевантаженого натовпу
шортистів небезпечно, а не безпечно. Такі монети НЕ відсіюються
зовсім — переходять у окрему категорію WATCH (цікаві самі по собі,
велика вірогідність активності маркет-мейкера/маніпуляції на тлі
скупчення шортистів — підхід до аналізу інший, не автоматичний
напрямок сигналу).

**Заборонені формулювання (analysis/CLAUDE.md, без винятку):**
результат — "ведмежий нахил" (direction="down") для category="short",
"спостереження" для category="watch" (без напрямку) — НІКОЛИ "шорти
це"/"входь у шорт". `reasons` — для аудиту, не інструкція.

Пороги — значення й пояснення кожного: `config.py`
(єдиний файл для ручного редагування, 2026-10-03).
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from config import (
    CRITICAL_NEGATIVE_FUNDING,
    HOURLY_PRICE_DROP_PCT,
    MIN_VOLUME_SPIKE_PCT,
    PUMP_THRESHOLD_PCT,
    RSI_OVERBOUGHT,
)


@dataclass
class ShortWatchSignal:
    symbol: str
    category: str  # "short" | "watch" | "none"
    direction: Optional[str]  # "down" лише для "short", інакше None
    reasons: list[str] = field(default_factory=list)


def screen_short_or_watch(
    symbol: str,
    pump_pct: Optional[float],
    oi_change_pct_after_pump: Optional[float],
    volume_spike_pct: Optional[float],
    rsi_value: Optional[float],
    funding_rate: Optional[Decimal],
    price_change_pct_1h: Optional[float] = None,
) -> ShortWatchSignal:
    """price_change_pct_1h — %-зміна ціни за ОСТАННЮ ГОДИНУ (не 24г,
    на відміну від pump_pct/volume_spike_pct) — передає лише
    monitor_candidates.py (погодинний моніторинг уже активних
    кандидатів); run_screening.py (перша детекція) не має "минулої
    години" для порівняння, там завжди None. Додано 2026-10-03 —
    docstring config.py:HOURLY_PRICE_DROP_PCT."""
    if pump_pct is None or pump_pct < PUMP_THRESHOLD_PCT:
        return ShortWatchSignal(
            symbol=symbol, category="none", direction=None,
            reasons=["немає пампу за вікном — сетап розвороту не застосовний"],
        )

    # Критичний ризик-гейт ПЕРШИМ, ще до перевірки решти критеріїв —
    # користувач, 2026-09-27: така монета НЕ фільтрується геть, а йде
    # в спостереження, незалежно від того, чи є інші ознаки виснаження.
    if funding_rate is not None and funding_rate <= CRITICAL_NEGATIVE_FUNDING:
        return ShortWatchSignal(
            symbol=symbol, category="watch", direction=None,
            reasons=[
                f"памп {pump_pct:.1f}% + критично негативний funding {funding_rate} "
                "— натовп шортистів уже в позиції, ризик short squeeze для нового шорту"
            ],
        )

    reasons = []
    if oi_change_pct_after_pump is None or oi_change_pct_after_pump >= 0:
        reasons.append("Open Interest не падає після пампу — леверидж-лонги ще не закриваються")
    if rsi_value is None or rsi_value < RSI_OVERBOUGHT:
        reasons.append(f"RSI не перегрітий (< {RSI_OVERBOUGHT})")

    # Підтвердження розвороту — АБО сплеск обсягу (24г), АБО різке
    # падіння ціни за останню годину (2026-10-03, живий кейс MAGMAUSDT
    # — config.py:HOURLY_PRICE_DROP_PCT) — досить ОДНОГО з двох, не
    # обов'язково обох, бо 24h-обсяг розмиває швидкий погодинний рух.
    volume_confirmed = volume_spike_pct is not None and volume_spike_pct >= MIN_VOLUME_SPIKE_PCT
    hourly_drop_confirmed = price_change_pct_1h is not None and price_change_pct_1h <= HOURLY_PRICE_DROP_PCT
    if not volume_confirmed and not hourly_drop_confirmed:
        reasons.append(
            f"немає підтвердження розвороту — ні сплеску обсягу (< {MIN_VOLUME_SPIKE_PCT}%), "
            f"ні різкого падіння ціни за годину (> {HOURLY_PRICE_DROP_PCT}%)"
        )

    if reasons:
        return ShortWatchSignal(symbol=symbol, category="none", direction=None, reasons=reasons)

    confirmation = "сплеск обсягу" if volume_confirmed else "різке падіння ціни за годину"
    if volume_confirmed and hourly_drop_confirmed:
        confirmation = "сплеск обсягу + різке падіння ціни за годину"
    return ShortWatchSignal(
        symbol=symbol,
        category="short",
        direction="down",
        reasons=[
            f"памп {pump_pct:.1f}% + OI падає {oi_change_pct_after_pump:.1f}% "
            f"+ {confirmation} + RSI перегрітий — ознаки виснаження руху"
        ],
    )
