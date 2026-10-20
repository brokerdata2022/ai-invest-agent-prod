#!/usr/bin/env python3
"""
Збір усіх ФАКТОРНИХ рядів (`config.FACTOR_SERIES`): специфічні дані
активів — запаси нафти й газу, потоки в ETF, он-чейн, погода, мідь —
разом із другими джерелами для звірки.

Кожне джерело збирається незалежно: збій одного не зупиняє решту. Але
наприкінці скрипт завершується з кодом 1, якщо впало хоч одне — джоба
має ГОЛОСНО провалитись (runner.py → Telegram), а не тихо видати
"зібрано 0" (урок GDELT 2026-10-05: тиждень мертвого потоку з
exit 0).

Використання:
    python run_collect_factors.py
    python run_collect_factors.py --metric us_crude_stocks
    python run_collect_factors.py --limit 400     # добрати історію
    python run_collect_factors.py --dry-run       # без запису в БД
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import config  # noqa: E402
from common.db import get_connection, insert_observations  # noqa: E402
from factor_sources import build_adapter, collection_plan  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--metric", choices=sorted(config.FACTOR_SERIES), default=None,
                        help="зібрати лише один факторний ряд (з його джерелами звірки)")
    parser.add_argument("--limit", type=int, default=None,
                        help="скільки точок історії (дефолт — свій для кожного адаптера)")
    parser.add_argument("--dry-run", action="store_true", help="лише показати, без запису в БД")
    args = parser.parse_args()

    load_dotenv()

    plan = collection_plan(args.metric)
    conn = None if args.dry_run else get_connection()
    failed: list[str] = []
    try:
        for source, metric_id in plan:
            label = f"{source}/{metric_id}"
            try:
                adapter = build_adapter(source, metric_id)
                kwargs = {"limit": args.limit} if args.limit else {}
                records = adapter.collect(**kwargs)
            except Exception as error:
                failed.append(label)
                logger.error("%s: збір провалився — %s: %s", label, type(error).__name__, error)
                continue

            newest = max(records, key=lambda r: r.observed_at)
            if args.dry_run:
                logger.info("%s: %d точок, остання %s = %s", label, len(records), newest.observed_at, newest.value)
                continue
            inserted = insert_observations(conn, records)
            logger.info(
                "%s: отримано %d, записано нових/ревізій %d; остання %s = %s",
                label, len(records), inserted, newest.observed_at, newest.value,
            )
    finally:
        if conn is not None:
            conn.close()

    logger.info("Готово: %d джерел, провалилось %d", len(plan), len(failed))
    if failed:
        logger.error("Провалились: %s", ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
