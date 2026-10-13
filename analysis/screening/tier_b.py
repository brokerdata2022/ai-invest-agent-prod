#!/usr/bin/env python3
"""
Tier B скринінгу акцій — фундаментальна якість (config.py).

Приймає на вхід тикери, що пройшли Tier A (analysis/screening/tier_a.py),
і фільтрує їх далі за SEC EDGAR даними (уже зібраними через
data-ingestion/collect_companies_universe.py). Як і tier_a.py — не
звертається до жодного зовнішнього API, тільки читає зі сховища
(CLAUDE.md, правило розділення data-ingestion/analysis).

Критерії (config.py, узгоджено 2026-09-20):
- NetIncomeLoss > 0 останні 4 квартали поспіль
- Revenues YoY > 10% (той самий квартал рік тому)
- EarningsPerShareDiluted YoY > 15%
- EntityCommonStockSharesOutstanding YoY <= 3% (толерантність до
  дилюції, не мінімум зростання)
- Liabilities / Assets < 0.6 (останній звітний період)

SQL-шар — batch-запити (screening._batch_db), не N+1 запит на тикер
(виправлено 2026-09-25, той самий фікс, що й у tier_a.py;
docs/decisions.md).

Використання:
    python analysis/screening/tier_b.py
"""

import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ANALYSIS_DIR)  # для "from screening.tier_a import ..."
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))
from common.db import get_connection  # noqa: E402
from screening.tier_a import run_tier_a  # noqa: E402
from screening._batch_db import batch_series  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Пороги — з ЄДИНОГО `config.py` у корені (рішення користувача
# 2026-10-04: один конфіг на весь агент). Тут лишається сама
# логіка воронки, не числа.
from config import (  # noqa: E402
    MAX_LIABILITIES_TO_ASSETS,
    MAX_SHARES_YOY,
    MIN_EPS_YOY,
    MIN_PROFITABLE_QUARTERS,
    MIN_REVENUE_YOY,
    YOY_TARGET_DAYS,
    YOY_TOLERANCE_DAYS,
)


@dataclass
class TierBResult:
    ticker: str
    revenue_yoy: Decimal
    eps_yoy: Decimal
    shares_yoy: Decimal
    liabilities_to_assets: Decimal


def all_positive_last_n(series: list[tuple[date, Decimal]], n: int = MIN_PROFITABLE_QUARTERS) -> bool:
    """series — [(дата, значення), ...] відсортовано за СПАДАННЯМ дати."""
    if len(series) < n:
        return False
    return all(val > 0 for _, val in series[:n])


def yoy_growth(series: list[tuple[date, Decimal]]) -> Optional[Decimal]:
    """(найновіше - рік_тому) / abs(рік_тому), або None якщо немає
    запису достатньо близько до рівно року тому. series — за
    СПАДАННЯМ дати, найновіший запис першим."""
    if not series:
        return None
    latest_date, latest_val = series[0]
    target = latest_date - timedelta(days=YOY_TARGET_DAYS)

    best_val, best_diff = None, None
    for d, v in series[1:]:
        diff = abs((d - target).days)
        if diff <= YOY_TOLERANCE_DAYS and (best_diff is None or diff < best_diff):
            best_val, best_diff = v, diff

    if best_val is None or best_val == 0:
        return None
    return (latest_val - best_val) / abs(best_val)


def passes_tier_b(
    net_income_series: list[tuple[date, Decimal]],
    revenue_series: list[tuple[date, Decimal]],
    eps_series: list[tuple[date, Decimal]],
    shares_series: list[tuple[date, Decimal]],
    liabilities: Optional[Decimal],
    assets: Optional[Decimal],
) -> tuple[bool, dict]:
    """Чиста функція, без звернень до БД — тестується окремо від SQL.
    Повертає (пройшов_чи_ні, деталі для звіту/діагностики)."""
    details: dict = {}

    if not all_positive_last_n(net_income_series):
        return False, details

    revenue_yoy = yoy_growth(revenue_series)
    details["revenue_yoy"] = revenue_yoy
    if revenue_yoy is None or revenue_yoy <= MIN_REVENUE_YOY:
        return False, details

    eps_yoy = yoy_growth(eps_series)
    details["eps_yoy"] = eps_yoy
    if eps_yoy is None or eps_yoy <= MIN_EPS_YOY:
        return False, details

    shares_yoy = yoy_growth(shares_series)
    details["shares_yoy"] = shares_yoy
    if shares_yoy is None or shares_yoy > MAX_SHARES_YOY:
        return False, details

    if liabilities is None or assets is None or assets == 0:
        return False, details
    liabilities_to_assets = liabilities / assets
    details["liabilities_to_assets"] = liabilities_to_assets
    if liabilities_to_assets >= MAX_LIABILITIES_TO_ASSETS:
        return False, details

    return True, details


def _series(conn, source: str, metric_id: str) -> list[tuple]:
    """Часовий ряд ОДНОГО metric_id — лишається для зворотної
    сумісності (tier_c.py й досі імпортує _series для одиничних
    викликів поза run_tier_b()); сам run_tier_b() тепер користується
    batch_series() нижче, не цією функцією, у своєму основному циклі."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT observed_at, value FROM v_observations_latest_revision "
            "WHERE source = %s AND metric_id = %s ORDER BY observed_at DESC",
            (source, metric_id),
        )
        return cur.fetchall()


def run_tier_b(tier_a_tickers: Optional[list[str]] = None) -> list[TierBResult]:
    conn = get_connection()
    try:
        if tier_a_tickers is None:
            tier_a_tickers = [r.ticker.lower() for r in run_tier_a()]
        else:
            tier_a_tickers = [t.lower() for t in tier_a_tickers]

        logger.info("Tier B: перевіряю %d тикерів, що пройшли Tier A", len(tier_a_tickers))

        # 6 batched-запитів на ВЕСЬ список тикерів замість 6*N окремих
        # (docs/decisions.md, 2026-09-25) — по одному на кожен концепт
        # SEC EDGAR, а не по одному на (тикер, концепт).
        net_income_by = batch_series(conn, "sec_edgar", [f"{t}_net_income" for t in tier_a_tickers])
        revenue_by = batch_series(conn, "sec_edgar", [f"{t}_revenue" for t in tier_a_tickers])
        eps_by = batch_series(conn, "sec_edgar", [f"{t}_eps_diluted" for t in tier_a_tickers])
        shares_by = batch_series(conn, "sec_edgar", [f"{t}_shares_outstanding" for t in tier_a_tickers])
        liabilities_by = batch_series(conn, "sec_edgar", [f"{t}_liabilities" for t in tier_a_tickers])
        assets_by = batch_series(conn, "sec_edgar", [f"{t}_assets" for t in tier_a_tickers])

        passed: list[TierBResult] = []
        for ticker in tier_a_tickers:
            net_income = net_income_by.get(f"{ticker}_net_income", [])
            revenue = revenue_by.get(f"{ticker}_revenue", [])
            eps = eps_by.get(f"{ticker}_eps_diluted", [])
            shares = shares_by.get(f"{ticker}_shares_outstanding", [])
            liabilities_series = liabilities_by.get(f"{ticker}_liabilities", [])
            assets_series = assets_by.get(f"{ticker}_assets", [])
            liabilities = liabilities_series[0][1] if liabilities_series else None
            assets = assets_series[0][1] if assets_series else None

            ok, details = passes_tier_b(net_income, revenue, eps, shares, liabilities, assets)
            if ok:
                passed.append(TierBResult(
                    ticker=ticker.upper(),
                    revenue_yoy=details["revenue_yoy"],
                    eps_yoy=details["eps_yoy"],
                    shares_yoy=details["shares_yoy"],
                    liabilities_to_assets=details["liabilities_to_assets"],
                ))

        logger.info("Tier B пройшли: %d з %d", len(passed), len(tier_a_tickers))
        return passed
    finally:
        conn.close()


if __name__ == "__main__":
    results = run_tier_b()
    results.sort(key=lambda r: r.revenue_yoy, reverse=True)

    print(f"\n{'Тикер':<8}{'Revenue YoY':>14}{'EPS YoY':>12}{'Shares YoY':>12}{'L/A':>8}")
    for r in results:
        print(
            f"{r.ticker:<8}{r.revenue_yoy * 100:>13.1f}%"
            f"{r.eps_yoy * 100:>11.1f}%{r.shares_yoy * 100:>11.1f}%"
            f"{r.liabilities_to_assets:>8.2f}"
        )
    print(f"\nВсього пройшли Tier B: {len(results)}")
