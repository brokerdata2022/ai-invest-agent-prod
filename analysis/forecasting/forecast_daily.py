#!/usr/bin/env python3
"""
LLM-прогноз для ДЕННИХ серій без календаря релізів — облігації
(`treasury_10y`/`treasury_2y`), ставка ФРС (`fed_funds_rate`), курс
USD/JPY (`usdjpy_fx_rate`). Рішення користувача 2026-10-04: "облігації
це обовязково".

## Чому окремий скрипт, а не update_forecasts.py

`update_forecasts.py` тригериться ПОДІЄЮ — 'detected'-рядком у
`release_log`, тобто фактом виходу релізу. У денних серій релізу не
існує (FRED не публікує для них release dates, `monitoring/
metric_sources.py:DAILY_ADAPTER_BY_METRIC`), тож той тригер їх ніколи
не зачепить. Тут тригер інший — РОЗКЛАД: раз на день після збору
(`orchestration/jobs.py:_macro_daily_series`). Спільна в обох
залишається сама робота: `forecast_metric.py:run_forecast()`.

## Ідемпотентність

Повторний прогін того самого дня не дублює й не марнує LLM-виклики
даремно лише частково: `save_forecast()` робить UPSERT по
(source, metric_id, method, based_on_observed_at), тож рядок не
дублюється — але сам виклик LLM станеться. Тому `--skip-existing`
(увімкнено за замовчуванням у джобі): якщо прогноз на ТОЙ САМИЙ
останній спостережений період уже є, показник пропускається без
звернення до LLM. Це важливо саме для денних серій: FRED публікує їх
із лагом день-два, тож кілька прогонів поспіль бачать ОДНЕ Й ТЕ САМЕ
останнє значення — без цієї перевірки ми платили б за однаковий
прогноз щодня.

Використання:
    python forecast_daily.py
    python forecast_daily.py --no-skip-existing   # перерахувати попри наявний
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "monitoring"))
sys.path.insert(0, _ANALYSIS_DIR)

from common.db import get_connection, fetch_recent  # noqa: E402
from llm_common import require_api_key  # noqa: E402
from metric_sources import DAILY_ADAPTER_BY_METRIC  # noqa: E402

from config import FORECAST_POLICY_RATE_METRICS  # noqa: E402

from forecasting._metric_source import resolve_source  # noqa: E402
from forecasting.forecast_metric import run_forecast  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def has_forecast_for_latest(conn, source: str, metric_id: str) -> bool:
    """Чи вже є прогноз, побудований на НАЙСВІЖІШОМУ спостереженні
    цього показника. Порівнюється саме `based_on_observed_at`, не дата
    створення: денна серія з лагом публікації кілька днів стоїть на
    тому самому значенні, і прогноз на нього вже зроблений.

    МЕТОД у фільтрі свідомо НЕ враховується (2026-10-05): з вибором
    методу за показником той самий прогноз може бути записаний як
    `llm`, `llm_unvalidated`, `naive_flat` чи `naive_llm_worse`, і
    перевірка лише по 'llm' щодня знаходила б "прогнозу нема" й
    робила новий. Виняток — `linear_trend`: це базова лінія бектесту,
    не висновок, і вона не має вважатись готовим прогнозом."""
    observations = fetch_recent(conn, source, metric_id, limit=1)
    if not observations:
        return False
    latest = observations[0]["observed_at"]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM metric_forecasts
            WHERE source = %s AND metric_id = %s
              AND method <> 'linear_trend'
              AND based_on_observed_at = %s
            """,
            (source, metric_id, latest),
        )
        return cur.fetchone() is not None


def forecast_daily_cycle(conn, skip_existing: bool = True) -> dict:
    """Повертає {"forecasted": [...], "skipped": [...], "failed": [...]}."""
    result: dict[str, list[str]] = {"forecasted": [], "skipped": [], "failed": []}

    for metric_id in sorted(DAILY_ADAPTER_BY_METRIC):
        # Ставки центробанків прогнозуються НА ЗАСІДАННЯ, не щодня
        # (config.py розділ 10, фідбек користувача 2026-10-11): між
        # засіданнями щоденна ефективна ставка стоїть на місці, і
        # денний прогноз для неї — витрата виклику на "як вчора".
        # Їхній календарний рядок заводить refresh_calendar, а збір
        # після рішення — check_releases.
        if metric_id in FORECAST_POLICY_RATE_METRICS:
            result["skipped"].append(metric_id)
            logger.info(
                "%s: ставка центробанку — прогноз робиться на засідання, не щодня",
                metric_id,
            )
            continue
        try:
            source = resolve_source(metric_id)
            if skip_existing and has_forecast_for_latest(conn, source, metric_id):
                logger.info(
                    "%s: прогноз на поточне останнє спостереження вже є — пропущено "
                    "(без виклику LLM)", metric_id,
                )
                result["skipped"].append(metric_id)
                continue

            forecast_id = run_forecast(conn, metric_id)
            if forecast_id is None:
                result["failed"].append(metric_id)
            else:
                result["forecasted"].append(metric_id)
        except Exception:
            # Один показник не валить цикл — той самий патерн, що
            # update_forecasts.py (з LLM у ланцюжку збій по одному
            # показнику звичайна справа).
            logger.error("%s: прогноз провалився", metric_id, exc_info=True)
            conn.rollback()
            result["failed"].append(metric_id)

    return result


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-skip-existing", dest="skip_existing", action="store_false",
        help="перерахувати прогноз навіть якщо він уже є на це спостереження",
    )
    parser.set_defaults(skip_existing=True)
    args = parser.parse_args()

    require_api_key("metric_forecast")

    conn = get_connection()
    try:
        result = forecast_daily_cycle(conn, skip_existing=args.skip_existing)
    finally:
        conn.close()

    logger.info(
        "Денні серії: спрогнозовано %d (%s), пропущено %d, провалено %d (%s)",
        len(result["forecasted"]), ", ".join(result["forecasted"]) or "-",
        len(result["skipped"]),
        len(result["failed"]), ", ".join(result["failed"]) or "-",
    )


if __name__ == "__main__":
    main()
