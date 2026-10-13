#!/usr/bin/env python3
"""
Автоматично оновлює LLM-прогноз (forecast_metric.py:run_forecast) для
показників, по яких monitoring/check_releases.py щойно задетектував нові
дані — замикає сигнал "щось нове вийшло" на прогноз наступного періоду
(docs/archive/plan-journal-2026-10-11.md, Фаза 3: "автоматичне оновлення прогнозів після кожного
релізу").

Незалежний консьюмер того самого 'detected'-сигналу (release_log), що
й analysis/expectations/compare_releases.py — обидва лише ЧИТАЮТЬ
'detected' рядки (monitoring/CLAUDE.md: "не плутати дані з'явились у
джерелі з дані оброблені"). Перехід у 'processed' лишається виключно
за compare_releases.py (monitoring/release_log.py) — ця джоба статус
НЕ чіпає, тому має бути в розкладі ДО compare_expectations, інакше
'detected'-рядки вже стануть 'processed' і зникнуть з вибірки.

Покриття: усі 15 показників календаря
(forecast_metric.py:FORECASTABLE_METRICS = monitoring/metric_sources.py).
Раніше було {"cpi"} — обмеження лінійної моделі, яка пройшла backtest
лише на одному показнику; LLM-прогноз такого обмеження не має
(докладніше — forecast_metric.py). Реальна кількість викликів LLM за
прогін дорівнює кількості ЩОЙНО вийшлих релізів (зазвичай 1-3), не 15.

Використання:
    python update_forecasts.py
"""

import logging
import os
import sys

from dotenv import load_dotenv

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "monitoring"))
sys.path.insert(0, _ANALYSIS_DIR)

from common.db import get_connection  # noqa: E402
from llm_common import require_api_key  # noqa: E402
from release_log import get_detected_entries  # noqa: E402

from forecasting.forecast_metric import FORECASTABLE_METRICS, run_forecast  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def update_forecasts_cycle(conn) -> list[str]:
    entries = get_detected_entries(conn)
    metric_ids = sorted({e["metric_id"] for e in entries if e["metric_id"] in FORECASTABLE_METRICS})

    updated = []
    for metric_id in metric_ids:
        try:
            forecast_id = run_forecast(conn, metric_id)
            if forecast_id is not None:
                updated.append(metric_id)
        except Exception:
            # Один показник не повинен валити весь цикл (той самий
            # патерн, що compare_releases.py/check_releases.py) — тим
            # паче з LLM у ланцюжку, де збійна відповідь чи таймаут по
            # одному показнику звичайна справа.
            logger.error("%s: оновлення прогнозу провалилось", metric_id, exc_info=True)
            conn.rollback()
    return updated


def main() -> None:
    load_dotenv()

    require_api_key("metric_forecast")

    conn = get_connection()
    try:
        updated = update_forecasts_cycle(conn)
        logger.info(
            "Оновлено прогнозів: %d (%s)", len(updated), ", ".join(updated) if updated else "-"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
