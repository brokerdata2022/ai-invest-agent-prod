#!/usr/bin/env python3
"""
Backtest трендової моделі (trend.py) на історичних даних одного
показника — критерій analysis/CLAUDE.md: "кожна модель прогнозування
повинна мати можливість прогнати на історичних даних, без цього
неможливо зрозуміти, чи модель взагалі має сенс".

⚠️ З 2026-10-04 `trend.py` — це БАЗОВА ЛІНІЯ, а не бойова модель:
прогноз формує LLM (`llm_forecast.py`, рішення користувача
2026-09-28). Цей скрипт лишається корисним саме тим, що БЕЗКОШТОВНИЙ —
проганяє всю історію без жодного виклику LLM, тож дає криву "скільки
взагалі можна витягнути з тренду" для порівняння. Backtest бойового
LLM-прогнозу — `backtest_llm.py` (там міряються обидві базові лінії
поруч із LLM).

На кожній точці історії (де попередніх спостережень досить,
--min-history) рахує прогноз ЛИШЕ з точок ДО неї (чесний
out-of-sample, не підглядає майбутнє) і порівнює середню абсолютну
похибку (MAE) лінійного тренду з наївною базовою лінією ("нічого не
змінилось"). Якщо тренд не б'є naive — для цього показника лінійна
модель поки не виправдана, це чесний результат, не привід одразу
ускладнювати.

Використання:
    python backtest.py --metric cpi
    python backtest.py --metric cpi --min-history 6 --limit 200
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))
sys.path.insert(0, _ANALYSIS_DIR)

from common.db import get_connection, fetch_recent  # noqa: E402

from forecasting._metric_source import resolve_source  # noqa: E402
from forecasting.trend import linear_trend_forecast, naive_forecast  # noqa: E402

from config import FORECAST_TREND_BASELINE_POINTS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def backtest_metric(metric_id: str, observations_desc: list[dict], min_history: int = 6) -> dict:
    """observations_desc — результат fetch_recent() (найновіше перше).
    Розвертає в хронологічний порядок (найстаріше перше, як очікує
    trend.py) і рахує MAE тренду vs naive на кожній точці, де
    попередньої історії досить (>= min_history)."""
    chronological = list(reversed(observations_desc))
    values = [float(o["value"]) for o in chronological]

    trend_errors = []
    naive_errors = []
    for i in range(min_history, len(values)):
        history = values[:i]
        actual = values[i]

        # Локальне вікно (2026-10-06): регресія через усю історію
        # після backfill (~285 точок) дає абсурд — та сама причина, що
        # в backtest_llm.py.
        trend_pred = linear_trend_forecast(history, window=FORECAST_TREND_BASELINE_POINTS)
        if trend_pred is not None:
            trend_errors.append(abs(actual - trend_pred))

        naive_pred = naive_forecast(history)
        if naive_pred is not None:
            naive_errors.append(abs(actual - naive_pred))

    return {
        "metric_id": metric_id,
        "n_points": len(trend_errors),
        "trend_mae": sum(trend_errors) / len(trend_errors) if trend_errors else None,
        "naive_mae": sum(naive_errors) / len(naive_errors) if naive_errors else None,
    }


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metric", required=True)
    parser.add_argument("--min-history", type=int, default=6)
    parser.add_argument("--limit", type=int, default=200, help="скільки останніх спостережень тягнути з БД")
    args = parser.parse_args()

    try:
        source = resolve_source(args.metric)
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    conn = get_connection()
    try:
        observations = fetch_recent(conn, source, args.metric, limit=args.limit)
        if len(observations) <= args.min_history:
            logger.error(
                "%s: замало історії (%d, потрібно > %d) — backtest неможливий",
                args.metric, len(observations), args.min_history,
            )
            sys.exit(1)

        result = backtest_metric(args.metric, observations, min_history=args.min_history)
        if result["trend_mae"] is None or result["naive_mae"] is None:
            # Можливо при малому --min-history: linear_trend_forecast()
            # повертає None, коли історії < 2 точок, тож список похибок
            # лишається порожнім. Без цієї перевірки порівняння нижче
            # падало з TypeError (None < None) замість зрозумілого
            # повідомлення.
            logger.error(
                "%s: жодної точки не вдалось перевірити (--min-history=%d замало для моделі) "
                "— збільште --min-history або наберіть більше історії",
                args.metric, args.min_history,
            )
            sys.exit(1)

        verdict = "тренд кращий" if result["trend_mae"] < result["naive_mae"] else "naive кращий (або не гірший)"
        logger.info(
            "%s: %d точок перевірено, MAE тренд=%.4g, MAE naive=%.4g — %s",
            result["metric_id"], result["n_points"], result["trend_mae"], result["naive_mae"], verdict,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
