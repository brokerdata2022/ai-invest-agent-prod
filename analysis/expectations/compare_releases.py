#!/usr/bin/env python3
"""
Порівнює факт із ринковим очікуванням для кожного release_log-рядка,
що `monitoring/check_releases.py` уже позначив 'detected' — і
переводить його в 'processed' (monitoring лише детектує, перехід у
'processed' — робота analysis/, docs release_log.py).

Детерміновано, без LLM (analysis/CLAUDE.md: числові порівняння —
звичайний код). Одиниці приводяться до спільного вигляду через
comparison_methods.py — деталі й причини там і в docs/decisions.md.

Використання:
    python compare_releases.py             # усі 'detected'
    python compare_releases.py --metric cpi  # тільки один metric_id (ручна перевірка)
"""

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date
from typing import Optional

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ANALYSIS_DIR)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "monitoring"))

from common.db import get_connection, fetch_recent  # noqa: E402
from release_log import get_detected_entries, mark_processed  # noqa: E402

from expectations.parse_expected import parse_expected_value  # noqa: E402
from expectations.comparison_methods import (  # noqa: E402
    COMPARISON_METHOD,
    RAW_UNIT_SCALE,
    REQUIRED_OBSERVATIONS,
    compare_to_calendar,
    compute_actual,
    forecast_in_comparison_units,
)
from expectations._db import fetch_prerelease_forecast, save_comparison  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class ComparisonResult:
    release_log_id: int
    source: str
    metric_id: str
    observed_at: date
    actual_value: float
    expected_value_raw: str
    expected_value_parsed: float
    surprise: float
    surprise_pct: Optional[float]
    comparison_method: str
    # ТРИ ЧИСЛА ПОРУЧ (2026-10-15): прогноз агента в тих самих
    # одиницях, що actual_value, і хто був ближчий до факту — ми чи
    # ринок. None = прогнозу на цей реліз не було (не «помилився»).
    forecast_value_parsed: Optional[float] = None
    forecast_method: Optional[str] = None
    forecast_verdict: Optional[str] = None


def compare_one(conn, entry: dict) -> Optional[ComparisonResult]:
    metric_id = entry["metric_id"]

    method = COMPARISON_METHOD.get(metric_id)
    if method is None:
        logger.warning("%s: немає методу порівняння в реєстрі — пропущено", metric_id)
        return None

    expected_raw = entry["expected_value"]
    expected_number = parse_expected_value(expected_raw)
    if expected_number is None:
        logger.info(
            "%s: немає прогнозу (expected_value=%r) — порівняння пропущено, реліз усе одно processed",
            metric_id, expected_raw,
        )
        return None

    scale = RAW_UNIT_SCALE.get(metric_id, 1.0)
    expected_scaled = expected_number * scale

    required = REQUIRED_OBSERVATIONS[method]
    observations = fetch_recent(conn, entry["source"], metric_id, limit=required)
    if len(observations) < required:
        logger.info(
            "%s: недостатньо історичних спостережень (%d/%d) — порівняння пропущено",
            metric_id, len(observations), required,
        )
        return None

    actual = compute_actual(method, observations)
    if actual is None:
        logger.info("%s: не вдалось обчислити факт (метод %s) — пропущено", metric_id, method)
        return None

    surprise = actual - expected_scaled
    surprise_pct = (surprise / abs(expected_scaled) * 100) if expected_scaled else None

    # --- прогноз АГЕНТА на цей самий реліз (2026-10-15) ---
    #
    # Третя точка зору поруч із фактом і ринковим очікуванням. Доти
    # прогноз жив в окремому повідомленні, і порівняти «ми проти
    # календарному» можна було лише SQL-запитом.
    observed_date = observations[0]["observed_at"]
    forecast_row = fetch_prerelease_forecast(conn, entry["source"], metric_id, observed_date)
    forecast_parsed = None
    forecast_method = None
    if forecast_row is not None:
        forecast_raw, forecast_method = forecast_row
        # Прогноз — РІВЕНЬ, порівняння — у одиницях ринкового квотування,
        # тож переводимо тим самим методом, що й факт.
        forecast_parsed = forecast_in_comparison_units(method, forecast_raw, observations)
        if forecast_parsed is None:
            logger.info(
                "%s: прогноз %s не переводиться в одиниці методу %s — пропущено",
                metric_id, forecast_raw, method,
            )
            forecast_method = None

    verdict = compare_to_calendar(actual, forecast_parsed, expected_scaled)
    if forecast_parsed is not None:
        logger.info(
            "%s: наш %.4f | календар %.4f | факт %.4f -> %s",
            metric_id, forecast_parsed, expected_scaled, actual, verdict,
        )
    else:
        logger.info("%s: прогнозу агента на цей реліз не було", metric_id)

    return ComparisonResult(
        release_log_id=entry["id"],
        source=entry["source"],
        metric_id=metric_id,
        observed_at=observations[0]["observed_at"],
        actual_value=actual,
        expected_value_raw=expected_raw,
        expected_value_parsed=expected_scaled,
        surprise=surprise,
        surprise_pct=surprise_pct,
        comparison_method=method,
        forecast_value_parsed=forecast_parsed,
        forecast_method=forecast_method,
        forecast_verdict=verdict,
    )


def run_comparison_cycle(conn, metric_filter: Optional[str] = None) -> list[ComparisonResult]:
    entries = get_detected_entries(conn)
    if metric_filter:
        entries = [e for e in entries if e["metric_id"] == metric_filter]

    results = []
    for entry in entries:
        try:
            result = compare_one(conn, entry)
            if result is not None:
                save_comparison(conn, result)
                logger.info(
                    "%s: факт %.4g vs очікування %.4g (сюрприз %+.4g)",
                    result.metric_id, result.actual_value, result.expected_value_parsed, result.surprise,
                )
                results.append(result)
            mark_processed(conn, entry["id"])
        except Exception:
            # Один показник не повинен валити весь цикл (той самий
            # патерн, що check_releases.py) — рядок лишається
            # 'detected', наступний прогін спробує знову.
            logger.error("%s: помилка під час порівняння", entry["metric_id"], exc_info=True)
            conn.rollback()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metric", default=None, help="обробити тільки один metric_id (ручна перевірка)")
    args = parser.parse_args()

    conn = get_connection()
    try:
        results = run_comparison_cycle(conn, metric_filter=args.metric)
        logger.info("Оброблено порівнянь: %d", len(results))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
