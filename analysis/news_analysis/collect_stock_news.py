#!/usr/bin/env python3
"""
Збір GDELT-новин для тикерів, що пройшли скринінг (Tier A→B→C,
analysis/screening/) — доповнення до data-ingestion/run_collect_news.py
--stream watchlist (той покриває тільки не-акційну частину
docs/watchlist.md). Обидва пишуть у raw_news зі stream="watchlist" —
та сама категорія "вже відібрані активи", просто двома окремими
GDELT-запитами (news/queries.py:build_stocks_query()).

Живе тут (analysis/), не в data-ingestion/, свідомо: список тикерів —
результат screening-логіки, а data-ingestion не повинен залежати від
analysis/ (rule 1, CLAUDE.md) — інакше зміна порогів скринінгу могла
б зламати збір даних. news/queries.py:build_stocks_query() лишається
чистою функцією (бере вже готовий {тикер: назва} від цього скрипта).

GDELT відхиляє занадто довгий query ("Your query was too short or too
long") — з 16 живими тикерами один об'єднаний запит (294 символи) не
пройшов (docs/decisions.md, 2026-09-25), тому тикери діляться на
кілька менших запитів (news/queries.py:batch_ticker_names()) з паузою
між ними (GDELT ліміт — 1 запит/5с).

За замовчуванням timespan=3d (docs/decisions.md, 2026-09-26) — без
обмеження GDELT віддає найновіші maxrecords збігів незалежно від
давності, включно з місяцями старими статтями, марними для "поточний
напрямок ринку".

Використання:
    python collect_stock_news.py
    python collect_stock_news.py --maxrecords 100
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
import requests

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_INGESTION_DIR = os.path.join(_ANALYSIS_DIR, "..", "data-ingestion")
sys.path.insert(0, _ANALYSIS_DIR)
sys.path.insert(0, _DATA_INGESTION_DIR)

from collect_universe import fetch_sp500_constituents  # noqa: E402
from common.db import get_connection  # noqa: E402
from common.news_db import insert_news_batch  # noqa: E402
from common.rate_limit import make_pacer  # noqa: E402
from news.gdelt_adapter import GdeltAdapter, GdeltError  # noqa: E402
from news.queries import batch_ticker_names, build_stocks_query  # noqa: E402
from screening.tier_c import run_tier_c  # noqa: E402

from config import (  # noqa: E402
    GDELT_MIN_REQUEST_INTERVAL_SECONDS,
    GDELT_THROTTLE_MAX_WAIT_SECONDS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Паузу між групами БІЛЬШЕ НЕ тримає цей скрипт: інтервал між
# запитами до GDELT тепер глобальний і міжпроцесний
# (`common/rate_limit.py`, config.py розділ 8). Локальна пауза тут була
# частиною збою 2026-10-05 — вона нічого не знала про запити двох
# інших джоб збору новин, що стартують у ті самі хвилини
# (docs/decisions.md).


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maxrecords", type=int, default=75)
    parser.add_argument(
        "--timespan", default="1d",
        help="скільки часу назад шукати (GDELT-формат, напр. 1d/3d) — "
             "без обмеження GDELT віддає найновіші maxrecords збігів "
             "БЕЗ огляду на давність, це можуть бути місяці старі статті",
    )
    args = parser.parse_args()

    tier_c_results = run_tier_c()
    tickers = sorted({r.ticker for r in tier_c_results if r.passed})
    if not tickers:
        logger.warning("Жодного тикера не пройшло Tier C — нема кого шукати в новинах.")
        sys.exit(0)
    logger.info("Тикери зі скринінгу (Tier C, пройшли): %d — %s", len(tickers), tickers)

    name_by_ticker = {c["symbol"]: c["name"] for c in fetch_sp500_constituents()}
    ticker_names = {ticker: name_by_ticker.get(ticker, "") for ticker in tickers}

    batches = batch_ticker_names(ticker_names)
    logger.info("Розбито на %d груп(и) запитів (ліміт довжини GDELT query)", len(batches))

    conn = get_connection()
    pacer = make_pacer(
        conn,
        api="gdelt",
        min_interval_seconds=GDELT_MIN_REQUEST_INTERVAL_SECONDS,
        max_wait_seconds=GDELT_THROTTLE_MAX_WAIT_SECONDS,
    )
    total_inserted = 0
    try:
        for i, batch in enumerate(batches, start=1):
            query = build_stocks_query(batch)
            logger.info("Група %d/%d (%d символів): %s", i, len(batches), len(query), query)

            adapter = GdeltAdapter(stream="watchlist", query=query, pacer=pacer)
            try:
                records = adapter.collect(maxrecords=args.maxrecords, timespan=args.timespan)
            except (requests.exceptions.RequestException, GdeltError):
                # Одна невдала група (вичерпаний retry-бюджет на 429/
                # не-JSON) не повинна губити статті вже зібраних груп —
                # записуємо одразу після кожної групи (нижче), а не
                # накопичуємо все в пам'яті до кінця циклу.
                logger.exception(
                    "Група %d/%d: пропускаю через помилку GDELT", i, len(batches)
                )
                continue

            logger.info("Група %d/%d: отримано %d статей", i, len(batches), len(records))
            if records:
                total_inserted += insert_news_batch(conn, records)

    finally:
        conn.close()

    logger.info("Готово: %d нових статей записано в raw_news", total_inserted)


if __name__ == "__main__":
    main()
