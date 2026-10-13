#!/usr/bin/env python3
"""
Пакетний збір фундаментальних даних (SEC EDGAR) для всього S&P 500 —
друге (поруч з quotes/collect_universe.py) джерело даних для
скринера (config.py).

CIK для кожного тикера береться з того самого constituents.csv, що й
для quotes/collect_universe.py (колонка CIK) — жодного окремого
тикер→CIK лукапу через company_tickers.json на кожен з 503 тикерів.

SecEdgarAdapter сам витримує паузу між запитами концептів усередині
одного тикера (~0.15с, SEC ліміт 10/сек) — тут додаткова пауза лише
між тикерами не потрібна, скрипт просто йде по списку підряд.

Використання:
    python collect_companies_universe.py
    python collect_companies_universe.py --limit 10   # тест на підмножині
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from collect_universe import fetch_sp500_constituents  # noqa: E402
from common.db import get_connection, insert_observations  # noqa: E402
from companies.sec_edgar_adapter import SecEdgarAdapter  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=None,
        help="забрати тільки перші N тикерів зі списку (для тестового прогону)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("SEC_EDGAR_USER_AGENT")
    if not api_key:
        logger.error("SEC_EDGAR_USER_AGENT не задано. Додайте його в .env.")
        sys.exit(1)

    logger.info("Завантажую список S&P 500 (з CIK)...")
    constituents = fetch_sp500_constituents()
    if args.limit:
        constituents = constituents[: args.limit]
    logger.info("Тикерів для збору: %d", len(constituents))

    conn = get_connection()
    ok, failed = 0, []
    try:
        for i, c in enumerate(constituents, start=1):
            ticker, cik = c["symbol"], c["cik"]
            logger.info("[%d/%d] %s (CIK %s)", i, len(constituents), ticker, cik)
            try:
                adapter = SecEdgarAdapter(api_key=api_key, ticker=ticker, cik=cik)
                records = adapter.collect()
                inserted = insert_observations(conn, records)
                logger.info("  -> %d записів, %d нових/змінених", len(records), inserted)
                ok += 1
            except Exception as exc:
                logger.error("  -> ПОМИЛКА для %s: %s", ticker, exc)
                failed.append(ticker)
    finally:
        conn.close()

    logger.info("Готово: %d успішно, %d з помилкою.", ok, len(failed))
    if failed:
        logger.warning("Тикери з помилкою: %s", ", ".join(failed))


if __name__ == "__main__":
    main()
