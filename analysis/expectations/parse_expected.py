"""
Парсинг `release_log.expected_value` — текст у форматі ForexFactory-фіду
(`monitoring/economic_calendar.py`): "0.6%", "-258B", "615K", "1.35M",
іноді просто число. Чиста функція, без БД — тестується на фікстурах
(той самий принцип, що `common/db.py:decide_revision`,
`monitoring/release_log.py:is_past_buffer`).
"""

import re
from typing import Optional

_SUFFIX_MULTIPLIER = {
    "K": 1_000,
    "M": 1_000_000,
    "B": 1_000_000_000,
}

# Знак (опційний) + цифри з крапкою (опційно) + опційний суфікс K/M/B
# (опційно) + опційний "%". Пробіли навколо — фід іноді додає їх.
_PATTERN = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*([KMB]?)\s*(%?)\s*$")


def parse_expected_value(text: Optional[str]) -> Optional[float]:
    """Повертає число в ПОВНИХ одиницях (K/M/B розгорнуто множенням),
    без знаку відсотка (сам факт "це відсоток" звідси не видно — то
    вже відповідальність analysis/expectations/comparison_methods.py,
    яка знає, яку величину означає прогноз для кожного metric_id).

    None — якщо текст порожній, відсутній чи не відповідає формату
    (реліз без консенсус-прогнозу, напр. mortgage_rate_30y — фід його
    не покриває, monitoring/economic_calendar.py:TITLE_PATTERNS)."""
    if not text:
        return None

    match = _PATTERN.match(text)
    if not match:
        return None

    number_str, suffix, _percent = match.groups()
    value = float(number_str)
    if suffix:
        value *= _SUFFIX_MULTIPLIER[suffix]
    return value
