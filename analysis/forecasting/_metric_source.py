"""
metric_id -> source (назва джерела в raw_observations) — тонка обгортка
над monitoring/metric_sources (адаптер несе .source), спільна для
backtest.py/backtest_llm.py/forecast_metric.py.

Два словники, не один (2026-10-04): `ADAPTER_BY_METRIC` — 15 показників
календаря релізів, `DAILY_ADAPTER_BY_METRIC` — денні серії без
календаря (облігації/ставка/USDJPY). Розділення живе в
monitoring/metric_sources.py і має причину саме там
(`refresh_calendar.py` ітерує ПЕРШИЙ словник) — тут вони рівноправні,
бо для прогнозу різниці немає: обидва набори це часові ряди в
raw_observations.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "monitoring"))

from metric_sources import ADAPTER_BY_METRIC, DAILY_ADAPTER_BY_METRIC  # noqa: E402


def resolve_source(metric_id: str) -> str:
    entry = ADAPTER_BY_METRIC.get(metric_id) or DAILY_ADAPTER_BY_METRIC.get(metric_id)
    if entry is None:
        known = sorted(set(ADAPTER_BY_METRIC) | set(DAILY_ADAPTER_BY_METRIC))
        raise ValueError(
            f"Невідомий metric_id={metric_id!r} — немає в monitoring/metric_sources.py. "
            f"Доступні: {known}"
        )
    adapter_class, _ = entry
    return adapter_class.source
