#!/usr/bin/env python3
"""
Tier A скринінгу акцій — ліквідність (config.py).

Читає вже зібрані дані з raw_observations (через views v_current_values/
v_observations_latest_revision) — Twelve Data для ціни/обсягу, SEC
EDGAR для shares_outstanding (капіталізація). Не звертається до
жодного зовнішнього API напряму — CLAUDE.md, правило #1:
analysis/ тільки читає зі сховища data-ingestion/, ніколи не
дублює логіку збору.

Критерії (config.py, узгоджено 2026-09-20):
- ціна > $10
- ринкова капіталізація > $10 млрд (ціна × shares_outstanding)
- середній доларовий обсяг торгів (3 міс, ~63 торгових дні) > $10 млн/день

Передумова: обидва data-ingestion/collect_universe.py (Twelve Data) і
data-ingestion/collect_companies_universe.py (SEC EDGAR) мають бути
прогнані на весь S&P 500 заздалегідь — інакше тикери без
shares_outstanding просто пропускаються з попередженням, а не
падають (див. _run у логах).

SQL-шар — batch-запити (screening._batch_db), не N+1 запит на тикер
(виправлено 2026-09-25 — до цього живий прогін на ~491 тикер займав
~9-10 хв через накладні витрати DISTINCT ON-views над TimescaleDB
hypertable з дрібним chunk-інтервалом на кожен окремий запит;
docs/decisions.md).

Використання:
    python analysis/screening/tier_a.py
"""

import logging
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ANALYSIS_DIR)  # для "from screening._batch_db import ..."
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))
from common.db import get_connection  # noqa: E402
from screening._batch_db import batch_latest_values, batch_series  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Пороги — з ЄДИНОГО `config.py` у корені (рішення користувача
# 2026-10-04: один конфіг на весь агент). Тут лишається сама
# логіка воронки, не числа.
from config import (  # noqa: E402
    LOOKBACK_DAYS,
    MIN_AVG_DOLLAR_VOLUME,
    MIN_MARKET_CAP,
    MIN_PRICE,
)


@dataclass
class TierAResult:
    ticker: str
    price: Decimal
    market_cap: Decimal
    avg_dollar_volume: Decimal


def passes_tier_a(price: Decimal, market_cap: Decimal, avg_dollar_volume: Optional[Decimal]) -> bool:
    """Чиста функція без звернень до БД — критерії Tier A окремо від
    того, як дані добуті, щоб можна було протестувати без живої БД."""
    if avg_dollar_volume is None:
        return False
    return (
        price > MIN_PRICE
        and market_cap > MIN_MARKET_CAP
        and avg_dollar_volume > MIN_AVG_DOLLAR_VOLUME
    )


def avg_dollar_volume_from_series(
    closes: list[tuple], volumes: list[tuple]
) -> Optional[Decimal]:
    """Чиста функція: середній доларовий обсяг із уже вибраних серій
    close/volume (кожна — [(observed_at, value), ...], до LOOKBACK_DAYS
    записів). Еквівалент SQL `AVG(close*volume)` по датах, спільних для
    обох серій (був INNER JOIN ON observed_at у попередній,
    по-тикерній версії запиту) — тепер рахується в Python, бо обидві
    серії вже вибрані ОДНИМ batched-запитом на всі тикери одразу."""
    volume_by_date = {observed_at: value for observed_at, value in volumes}
    products = [close * volume_by_date[d] for d, close in closes if d in volume_by_date]
    if not products:
        return None
    return sum(products) / Decimal(len(products))


def _tickers_with_quotes(conn) -> list[str]:
    suffix = "_close"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT metric_id FROM v_current_values "
            "WHERE source = 'twelvedata' AND metric_id LIKE '%%' || %s",
            (suffix,),
        )
        return sorted(row[0][: -len(suffix)] for row in cur.fetchall())


def run_tier_a() -> list[TierAResult]:
    conn = get_connection()
    try:
        tickers = _tickers_with_quotes(conn)
        logger.info("Тикерів з котируваннями (Twelve Data): %d", len(tickers))

        close_ids = [f"{t}_close" for t in tickers]
        volume_ids = [f"{t}_volume" for t in tickers]
        shares_ids = [f"{t}_shares_outstanding" for t in tickers]

        # 4 batched-запити на ВЕСЬ список тикерів замість 3*N окремих —
        # це і є фікс N+1 (docs/decisions.md, 2026-09-25).
        prices = batch_latest_values(conn, "twelvedata", close_ids)
        shares_by_ticker = batch_latest_values(conn, "sec_edgar", shares_ids)
        closes_by_ticker = batch_series(conn, "twelvedata", close_ids, limit_per_metric=LOOKBACK_DAYS)
        volumes_by_ticker = batch_series(conn, "twelvedata", volume_ids, limit_per_metric=LOOKBACK_DAYS)

        passed: list[TierAResult] = []
        skipped_no_shares = []
        for ticker in tickers:
            price = prices.get(f"{ticker}_close")
            shares = shares_by_ticker.get(f"{ticker}_shares_outstanding")
            if price is None:
                continue
            if shares is None:
                skipped_no_shares.append(ticker)
                continue

            market_cap = price * shares
            avg_dollar_volume = avg_dollar_volume_from_series(
                closes_by_ticker.get(f"{ticker}_close", []),
                volumes_by_ticker.get(f"{ticker}_volume", []),
            )

            if passes_tier_a(price, market_cap, avg_dollar_volume):
                passed.append(TierAResult(
                    ticker=ticker.upper(), price=price,
                    market_cap=market_cap, avg_dollar_volume=avg_dollar_volume,
                ))

        logger.info("Tier A пройшли: %d з %d", len(passed), len(tickers))
        if skipped_no_shares:
            preview = ", ".join(skipped_no_shares[:20])
            more = "..." if len(skipped_no_shares) > 20 else ""
            logger.warning(
                "Пропущено (немає shares_outstanding з SEC EDGAR): %d — %s%s. "
                "Прогнано collect_companies_universe.py?",
                len(skipped_no_shares), preview, more,
            )
        return passed
    finally:
        conn.close()


if __name__ == "__main__":
    results = run_tier_a()
    results.sort(key=lambda r: r.market_cap, reverse=True)

    print(f"\n{'Тикер':<8}{'Ціна':>12}{'Капіталізація':>20}{'Сер.$ обсяг/день':>22}")
    for r in results:
        print(f"{r.ticker:<8}{r.price:>12,.2f}{r.market_cap:>20,.0f}{r.avg_dollar_volume:>22,.0f}")
    print(f"\nВсього пройшли Tier A: {len(results)}")
