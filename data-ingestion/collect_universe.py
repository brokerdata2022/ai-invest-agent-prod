#!/usr/bin/env python3
"""
Пакетний збір котирувань (Twelve Data) для всього S&P 500 — вхід для
скринера (config.py). На відміну від run_collect.py
(один тикер за раз, дебаг-інструмент), цей скрипт проходить весь
universe і призначений для реального (щокварталу, синхронно з 10-Q —
config.py) запуску.

Джерело списку тикерів: constituents.csv,
github.com/datasets/s-and-p-500-companies (docs/decisions.md,
2026-09-20). Формат перевірено живим запитом 2026-09-20: колонка
"Symbol", 503 тикери, 2 з крапкою в класі акцій (BRK.B, BF.B).

Поважає ліміт Twelve Data free tier (~8 запитів/хв) — пауза між
запитами. Повний прогін на ~500 тикерів займає ~65+ хв.

Використання:
    python collect_universe.py
    python collect_universe.py --limit 10   # тест на підмножині
"""

import argparse
import csv
import io
import logging
import os
import sys
import time

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.db import get_connection, insert_observations  # noqa: E402
from quotes.twelvedata_adapter import TwelveDataAdapter  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CONSTITUENTS_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

# Twelve Data free tier: ~8 запитів/хв. 8с — трохи більше за
# теоретичний мінімум (60/8=7.5с), запас на неточність ліміту.
SECONDS_BETWEEN_REQUESTS = 8


def fetch_sp500_constituents() -> list[dict]:
    """Повертає [{"symbol": "AAPL", "cik": "320193", "name": "..."}, ...].

    CIK беремо звідси, а не з окремого SEC company_tickers.json — файл
    уже містить колонку CIK (підтверджено живим тестом 2026-09-20),
    тож для companies/ (SEC EDGAR) не потрібен окремий тикер→CIK
    лукап на кожен з 503 тикерів.
    """
    response = requests.get(CONSTITUENTS_URL, timeout=30)
    response.raise_for_status()

    reader = csv.DictReader(io.StringIO(response.text))
    required = {"Symbol", "CIK"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError(
            f"Неочікуваний формат constituents.csv — немає колонок {required}. "
            f"Реальні колонки: {reader.fieldnames!r}. Схема датасету могла "
            f"змінитись — перевірте вручну: {CONSTITUENTS_URL}"
        )

    constituents = []
    for row in reader:
        symbol = (row.get("Symbol") or "").strip()
        cik = (row.get("CIK") or "").strip()
        if symbol and cik:
            constituents.append({"symbol": symbol, "cik": cik, "name": row.get("Security", "")})
    if not constituents:
        raise ValueError("constituents.csv завантажено, але жодного тикера не знайдено.")
    return constituents


def fetch_sp500_tickers() -> list[str]:
    return [c["symbol"] for c in fetch_sp500_constituents()]


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=None,
        help="забрати тільки перші N тикерів зі списку (для тестового прогону)",
    )
    parser.add_argument(
        "--bars", type=int, default=70,
        help="скільки останніх днів забирати на тикер (за замовчуванням 70 — "
             "з запасом понад ~63 торгових дні на 3-місячний Tier A критерій)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("TWELVEDATA_API_KEY")
    if not api_key:
        logger.error("TWELVEDATA_API_KEY не задано. Додайте його в .env.")
        sys.exit(1)

    logger.info("Завантажую список S&P 500 з %s...", CONSTITUENTS_URL)
    tickers = fetch_sp500_tickers()
    if args.limit:
        tickers = tickers[: args.limit]
    logger.info("Тикерів для збору: %d (оцінка часу: ~%d хв)", len(tickers), len(tickers) * SECONDS_BETWEEN_REQUESTS // 60)

    conn = get_connection()
    ok, failed = 0, []
    try:
        for i, ticker in enumerate(tickers, start=1):
            logger.info("[%d/%d] %s", i, len(tickers), ticker)
            try:
                adapter = TwelveDataAdapter(api_key=api_key, ticker=ticker)
                records = adapter.collect(limit=args.bars)
                inserted = insert_observations(conn, records)
                logger.info("  -> %d записів, %d нових/змінених", len(records), inserted)
                ok += 1
            except Exception as exc:
                logger.error("  -> ПОМИЛКА для %s: %s", ticker, exc)
                failed.append(ticker)

            if i < len(tickers):
                time.sleep(SECONDS_BETWEEN_REQUESTS)
    finally:
        conn.close()

    logger.info("Готово: %d успішно, %d з помилкою.", ok, len(failed))
    if failed:
        logger.warning("Тикери з помилкою: %s", ", ".join(failed))


if __name__ == "__main__":
    main()
