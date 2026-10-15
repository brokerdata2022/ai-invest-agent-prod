#!/usr/bin/env python3
"""
Збір календаря звітності компаній у `earnings_calendar`
(джерело — `companies/nasdaq_earnings_adapter.py`).

**Чому фільтр по тикерах, які ми ведемо.** Nasdaq віддає ВЕСЬ ринок на
кожну дату (живий замір 2026-10-15: 57 компаній на 20.10). Зберігати
все означало б наповнювати таблицю компаніями, яких агент не бачить і
по яких не має ні цін, ні фундаменталу — тобто каталізатором, який
нікуди не підключиться. Фільтр — `config.EARNINGS_ONLY_TRACKED_TICKERS`,
а «ведемо» означає «є котирування в `raw_observations`».

**Чому UPSERT, а не append.** Дата звіту ПЕРЕНОСИТЬСЯ компаніями, і
новіша відповідь джерела авторитетніша за старішу. Ключ —
(ticker, report_date), тож перенос у межах того самого дня оновлює
рядок, а перенос на інший день додає новий; старий лишається, доки не
пройде — це видно в логі й не ламає каталізатор, бо той дивиться лише
вперед.

Використання:
    python run_collect_earnings.py
    python run_collect_earnings.py --days 30
    python run_collect_earnings.py --all-tickers   # без фільтра (діагностика)
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
sys.path.insert(0, _REPO_ROOT)

from common.db import get_connection  # noqa: E402
from companies.nasdaq_earnings_adapter import collect  # noqa: E402

import config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Збирає календар звітності компаній (Nasdaq) у earnings_calendar — "
    "прицільний каталізатор для акцій зі скринінгу."
)


def tracked_tickers(conn) -> set:
    """Тикери, по яких у нас узагалі є котирування. `_close`-метрики
    `twelvedata` — те, на чому стоїть скринінг (`quotes_universe_refresh`)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT upper(replace(metric_id, '_close', ''))
            FROM raw_observations
            WHERE source = 'twelvedata' AND metric_id LIKE '%%_close'
            """
        )
        return {row[0] for row in cur.fetchall()}


def save(conn, records: list[dict]) -> int:
    stored = 0
    with conn.cursor() as cur:
        for record in records:
            cur.execute(
                """
                INSERT INTO earnings_calendar (
                    ticker, report_date, report_time, eps_forecast, company_name, source, fetched_at
                ) VALUES (
                    %(ticker)s, %(report_date)s, %(report_time)s, %(eps_forecast)s,
                    %(company_name)s, %(source)s, now()
                )
                ON CONFLICT (ticker, report_date) DO UPDATE SET
                    report_time  = EXCLUDED.report_time,
                    eps_forecast = EXCLUDED.eps_forecast,
                    company_name = COALESCE(EXCLUDED.company_name, earnings_calendar.company_name),
                    fetched_at   = now()
                """,
                record,
            )
            stored += 1
    conn.commit()
    return stored


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--days", type=int, default=config.EARNINGS_CALENDAR_HORIZON_DAYS,
        help=f"горизонт у днях (дефолт {config.EARNINGS_CALENDAR_HORIZON_DAYS})",
    )
    parser.add_argument(
        "--all-tickers", action="store_true",
        help="не фільтрувати по тикерах, які ми ведемо (діагностика джерела)",
    )
    args = parser.parse_args()

    records = collect(horizon_days=args.days)
    logger.info("Nasdaq: %d записів календаря на %d днів", len(records), args.days)
    if not records:
        logger.warning(
            "Джерело не дало жодного запису — перевірте доступність "
            "api.nasdaq.com (неофіційний endpoint, без ключа)"
        )
        return

    conn = get_connection()
    try:
        if config.EARNINGS_ONLY_TRACKED_TICKERS and not args.all_tickers:
            tracked = tracked_tickers(conn)
            before = len(records)
            records = [r for r in records if r["ticker"] in tracked]
            logger.info(
                "Фільтр по наших тикерах: %d з %d (ведемо %d тикерів)",
                len(records), before, len(tracked),
            )

        if not records:
            logger.info("Жодна з наших компаній не звітує в цьому вікні")
            return

        stored = save(conn, records)
        logger.info("Збережено записів календаря звітності: %d", stored)
        for record in sorted(records, key=lambda r: (r["report_date"], r["ticker"]))[:20]:
            logger.info(
                "  %s %s%s%s",
                record["report_date"], record["ticker"],
                f" ({record['report_time']})" if record["report_time"] else "",
                f", прогноз EPS {record['eps_forecast']}" if record["eps_forecast"] is not None else "",
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
