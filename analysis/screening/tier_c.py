"""Tier C скринінгу: оцінка вартості (valuation).

Третій і останній рівень hard-фільтрів перед composite score
ранжуванням (порядок: Tier A -> Tier B -> Tier C, кожен наступний
рівень працює тільки з тикерами, що пройшли попередній).

Критерії (config.py):
    - P/E (price / TTM EPS) в діапазоні (MIN_PE, MAX_PE)
    - P/S (market cap / TTM revenue) < MAX_PS
    - PEG (P/E / (eps_growth_yoy * 100)) < MAX_PEG

TTM (trailing twelve months) = сума останніх 4 кварталів, не останнє
квартальне значення -- аналогічно до ttm-логіки, яка вже
використовується непрямо через YoY-порівняння в tier_b.py.

eps_growth_yoy береться з результату Tier B (TierBResult.eps_yoy) --
PEG НЕ рахує зростання EPS повторно, бо воно вже обчислене і
провалідоване (>= MIN_EPS_YOY) на попередньому рівні.

Джерело серій (eps_diluted, revenue) -- ті самі, що вже зібрані
sec_edgar-адаптером і використані в tier_b.py; для їх читання
використовується batch_series() з screening._batch_db (ОДИН запит на
весь список тикерів, а не по одному на тикер -- фікс N+1, 2026-09-25,
docs/decisions.md; той самий модуль уже використовують tier_a.py й
tier_b.py).
"""

import logging
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ANALYSIS_DIR)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))

# Примітка: імпорти common.db / screening.tier_a / screening.tier_b
# зроблені лаконічно всередині run_tier_c(), а не тут, на рівні модуля.
# Так ttm_sum() і passes_tier_c() (чисті функції) можна імпортувати й
# тестувати без БД і без залежності від tier_a.py/tier_b.py -- так само,
# як у tier_a.py/tier_b.py тестується їхня чиста логіка.

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Пороги — з ЄДИНОГО `config.py` у корені (рішення користувача
# 2026-10-04: один конфіг на весь агент). Тут лишається сама
# логіка воронки, не числа.
from config import (  # noqa: E402
    MAX_PE,
    MAX_PEG,
    MAX_PS,
    MIN_PE,
    TTM_QUARTERS,
)


@dataclass
class TierCResult:
    ticker: str
    price: Decimal
    market_cap: Decimal
    eps_ttm: Optional[Decimal]
    revenue_ttm: Optional[Decimal]
    pe: Optional[Decimal]
    ps: Optional[Decimal]
    peg: Optional[Decimal]
    passed: bool
    fail_reason: Optional[str] = None


def ttm_sum(series: list[tuple], n: int = TTM_QUARTERS) -> Optional[Decimal]:
    """Сума останніх n значень квартальної серії (найновіші перші).

    series -- список (observed_at, value), відсортований спаданням дати
    (той самий формат, що повертає _series() з tier_b.py).
    Повертає None, якщо серія коротша за n (недостатньо історії для TTM).
    """
    if len(series) < n:
        return None
    return sum(v for _, v in series[:n])


def passes_tier_c(
    price: Decimal,
    market_cap: Decimal,
    eps_ttm: Optional[Decimal],
    revenue_ttm: Optional[Decimal],
    eps_growth_yoy: Optional[Decimal],
) -> tuple[bool, dict]:
    """Чиста функція перевірки критеріїв Tier C. Рання зупинка.

    details накопичується тільки до точки провалу (як у passes_tier_b).
    """
    details: dict = {}

    if eps_ttm is None or eps_ttm <= 0:
        details["fail_reason"] = "eps_ttm_missing_or_negative"
        return False, details

    pe = price / eps_ttm
    details["pe"] = pe
    if not (MIN_PE < pe < MAX_PE):
        details["fail_reason"] = "pe_out_of_range"
        return False, details

    if revenue_ttm is None or revenue_ttm <= 0:
        details["fail_reason"] = "revenue_ttm_missing_or_negative"
        return False, details

    ps = market_cap / revenue_ttm
    details["ps"] = ps
    if ps >= MAX_PS:
        details["fail_reason"] = "ps_too_high"
        return False, details

    if eps_growth_yoy is None or eps_growth_yoy <= 0:
        details["fail_reason"] = "eps_growth_missing_or_negative"
        return False, details

    peg = pe / (eps_growth_yoy * 100)
    details["peg"] = peg
    if peg >= MAX_PEG:
        details["fail_reason"] = "peg_too_high"
        return False, details

    return True, details


def run_tier_c(tier_a_results=None, tier_b_results=None) -> list[TierCResult]:
    """Запускає Tier C поверх результатів Tier A і Tier B.

    Якщо tier_a_results/tier_b_results не передані -- виконує run_tier_a()
    і run_tier_b() сам (для самостійного запуску як __main__).
    """
    from common.db import get_connection
    from screening.tier_a import run_tier_a
    from screening.tier_b import run_tier_b
    from screening._batch_db import batch_series

    if tier_a_results is None:
        tier_a_results = run_tier_a()
    if tier_b_results is None:
        # tier_a_results (вище) передається як готовий список тикерів,
        # щоб run_tier_b() не перераховував Tier A ще раз із нуля --
        # той самий фікс редундантного перерахунку, що й у
        # composite_score.py (docs/decisions.md, 2026-09-25).
        tier_b_results = run_tier_b(tier_a_tickers=[r.ticker for r in tier_a_results])

    tier_a_by_ticker = {r.ticker: r for r in tier_a_results}

    results: list[TierCResult] = []
    conn = get_connection()
    try:
        tickers = [b.ticker.lower() for b in tier_b_results]
        # 2 batched-запити на ВЕСЬ список тикерів замість 2*N окремих
        # (docs/decisions.md, 2026-09-25).
        eps_by = batch_series(conn, "sec_edgar", [f"{t}_eps_diluted" for t in tickers])
        revenue_by = batch_series(conn, "sec_edgar", [f"{t}_revenue" for t in tickers])

        for b in tier_b_results:
            ticker = b.ticker
            a = tier_a_by_ticker.get(ticker)
            if a is None:
                logger.warning("Тикер %s є в Tier B, але відсутній у Tier A -- пропускаю", ticker)
                continue

            eps_series = eps_by.get(f"{ticker.lower()}_eps_diluted", [])
            revenue_series = revenue_by.get(f"{ticker.lower()}_revenue", [])

            eps_ttm = ttm_sum(eps_series)
            revenue_ttm = ttm_sum(revenue_series)

            passed, details = passes_tier_c(
                price=a.price,
                market_cap=a.market_cap,
                eps_ttm=eps_ttm,
                revenue_ttm=revenue_ttm,
                eps_growth_yoy=getattr(b, "eps_yoy", None),
            )

            results.append(
                TierCResult(
                    ticker=ticker,
                    price=a.price,
                    market_cap=a.market_cap,
                    eps_ttm=eps_ttm,
                    revenue_ttm=revenue_ttm,
                    pe=details.get("pe"),
                    ps=details.get("ps"),
                    peg=details.get("peg"),
                    passed=passed,
                    fail_reason=details.get("fail_reason"),
                )
            )
    finally:
        conn.close()

    return results


if __name__ == "__main__":
    all_results = run_tier_c()
    passed_results = [r for r in all_results if r.passed]

    logger.info(
        "Tier C пройшли: %d з %d (з тих, що пройшли Tier B)",
        len(passed_results),
        len(all_results),
    )

    print(f"{'Ticker':<8}{'Price':>10}{'P/E':>8}{'P/S':>8}{'PEG':>8}")
    for r in sorted(passed_results, key=lambda x: x.ticker):
        pe_s = f"{r.pe:.1f}" if r.pe is not None else "-"
        ps_s = f"{r.ps:.1f}" if r.ps is not None else "-"
        peg_s = f"{r.peg:.2f}" if r.peg is not None else "-"
        print(f"{r.ticker:<8}{float(r.price):>10.2f}{pe_s:>8}{ps_s:>8}{peg_s:>8}")
