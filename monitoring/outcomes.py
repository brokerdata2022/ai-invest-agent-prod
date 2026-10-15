"""
Вердикт по одному рішенню агента — чиста функція (rule 1, CLAUDE.md):
SQL — `_outcomes_db.py`, проводка — `evaluate_outcomes.py`.

**Навіщо це взагалі** (2026-10-15, рішення користувача). Агент видавав
списки й вердикти й ніколи не перевіряв, що з них вийшло. Єдина
постфактум-оцінка в проєкті стосувалась прогнозів показників
(`backtest_verdicts`: ШІ проти naive), тож ваги й пороги в `config.py`
для скринінгів і списку торгів лишались припущеннями.

**Чому смуга «нічого не сталось»** (`OUTCOME_FLAT_BAND_PCT`). Без неї
будь-який шум у ±0.1% зараховувався б як «правильний напрямок» — hit
rate вийшов би ~50% на випадкових даних і виглядав як метрика. Рух
менший за смугу — `flat`: ні перемога, ні поразка.

**Чому `no_claim` окремо від `flat`.** `watch` і `unclear` — це НЕ
прогноз напрямку, а запрошення подивитись. Зарахувати їх у відсоток
влучань (у будь-який бік) означало б міряти те, чого агент не
стверджував. Такі рядки пишуться (рух по них цікавий сам собою), але в
hit rate не входять.
"""

from typing import Optional

CORRECT = "correct"
INCORRECT = "incorrect"
FLAT = "flat"
NO_CLAIM = "no_claim"
UNAVAILABLE = "unavailable"

# Напрямки, що є ТВЕРДЖЕННЯМ про рух. Решта (`watch`, `unclear`,
# `neutral`, порожнє) — спостереження без напрямку.
_UP = ("up", "long", "bullish")
_DOWN = ("down", "short", "bearish")


def is_directional(direction: Optional[str]) -> bool:
    """Чи рішення взагалі стверджує напрямок."""
    if not direction:
        return False
    return direction.lower() in _UP + _DOWN


def realized_pct(price_at_decision, price_at_eval) -> Optional[float]:
    """Фактичний рух у %, або None якщо котирування не знайшлось."""
    if price_at_decision is None or price_at_eval is None:
        return None
    start = float(price_at_decision)
    if start == 0:
        return None
    return (float(price_at_eval) - start) / abs(start) * 100


def verdict_for(direction: Optional[str], move_pct: Optional[float], flat_band_pct: float) -> str:
    """Вердикт по рішенню. Порядок перевірок важливий: спершу
    відсутність даних, потім відсутність твердження, і лише тоді
    порівняння з рухом."""
    if move_pct is None:
        return UNAVAILABLE
    if not is_directional(direction):
        return NO_CLAIM
    if abs(move_pct) < flat_band_pct:
        return FLAT
    claimed_up = direction.lower() in _UP
    moved_up = move_pct > 0
    return CORRECT if claimed_up == moved_up else INCORRECT


def hit_rate(verdicts: list[str]) -> Optional[float]:
    """Частка влучань серед рішень, де був НАПРЯМОК і РУХ, у %.

    `flat`, `no_claim` і `unavailable` у знаменник не входять: перші
    два не є твердженням про рух, третє — наша власна відсутність
    даних, а не помилка агента. `None`, якщо судити ні по чому."""
    scored = [v for v in verdicts if v in (CORRECT, INCORRECT)]
    if not scored:
        return None
    return scored.count(CORRECT) / len(scored) * 100
