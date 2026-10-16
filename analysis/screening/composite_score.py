"""Composite score: фінальне ранжування тикерів, що пройшли Tier A/B/C.

Формула (config.py):
    score = 0.35 * percentile(revenue_growth)
          + 0.30 * percentile(eps_growth)
          + 0.20 * percentile(-P/E)
          + 0.15 * percentile(avg_dollar_volume)

Це НЕ ще один hard-фільтр (як Tier A/B/C) -- composite score нічого не
відсіює, він тільки ранжує список тикерів, що вже пройшли всі три рівні
скринінгу, від найпривабливішого до найменш привабливого. Скільки з
верху списку брати (5, 10, 20) -- рішення користувача, а не жорсткий
поріг у коді, тому run_composite_score() за замовчуванням повертає
ПОВНИЙ ранжований список; --top лише обрізає ВИВІД у __main__.

Percentile тут -- percentile RANK всередині самого набору тикерів, що
пройшли Tier C (не percentile відносно всього S&P 500): наприклад,
якщо в списку 16 тикерів і в тикера X eps_growth вище, ніж у 12 з 15
інших, його percentile(eps_growth) = 12/15 ≈ 0.80. Це узгоджено з
задумом критеріїв: порівнюємо тільки вже відфільтрованих кандидатів
між собою, а не з усім ринком.

-P/E: нижчий P/E -- краще (дешевша оцінка відносно прибутку), тому
percentile рахується від'ємного P/E (percentile_rank(-pe)), а не
від самого P/E.
"""

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ANALYSIS_DIR)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))

from common.db import get_connection  # noqa: E402
from screening._batch_db import batch_series, calendar_days_for  # noqa: E402
from screening._results_db import save_screening_run  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Пороги — з ЄДИНОГО `config.py` у корені (рішення користувача
# 2026-10-04: один конфіг на весь агент). Тут лишається сама
# логіка воронки, не числа.
from config import (  # noqa: E402
    MOMENTUM_DAYS,
    SCREENING_PRICE_CALENDAR_BUFFER_DAYS,
    WEIGHT_AVG_DOLLAR_VOLUME,
    WEIGHT_EPS_GROWTH,
    WEIGHT_MOMENTUM,
    WEIGHT_NEG_PE,
    WEIGHT_REVENUE_GROWTH,
)


@dataclass
class CompositeResult:
    ticker: str
    score: Decimal
    revenue_growth: Decimal
    eps_growth: Decimal
    pe: Decimal
    avg_dollar_volume: Decimal
    revenue_growth_pct: Decimal
    eps_growth_pct: Decimal
    neg_pe_pct: Decimal
    avg_dollar_volume_pct: Decimal
    momentum_pct_change: Decimal
    momentum_pct: Decimal
    # Контекст для виводу (reporting/screening_notify.py), не впливають
    # на сам score — заповнюються ПІСЛЯ ранжування, окремим
    # enrich_with_report_context() (живий фідбек користувача: score/
    # revenue/eps/pe/avg_dollar_volume нічого не каже без контексту в
    # Telegram-повідомленні, реальна потреба — тикер/назва/зміна 24г).
    company_name: str = ""
    price_change_24h_pct: Optional[Decimal] = None
    price_date: Optional[date] = None  # дата останнього закриття, яке й дає %-зміну
    volume_change_24h_pct: Optional[Decimal] = None


def percentile_ranks(values: list[Decimal]) -> list[Decimal]:
    """Percentile rank (0..1) кожного значення всередині власного списку.

    Метод середнього рангу (average rank) для коректної обробки
    однакових значень (tie): rank(x) = (к-сть менших) + (к-сть рівних - 1) / 2,
    percentile = rank / (n - 1).

    n <= 1 -> усі отримують 0.5 (нема з чим порівнювати).
    """
    n = len(values)
    if n <= 1:
        return [Decimal("0.5") for _ in values]

    result = []
    for v in values:
        less = sum(1 for x in values if x < v)
        equal = sum(1 for x in values if x == v)
        rank = Decimal(less) + (Decimal(equal) - 1) / 2
        result.append(rank / (n - 1))
    return result


def run_composite_score(
    tier_a_results=None, tier_b_results=None, tier_c_results=None
) -> list[CompositeResult]:
    from screening.tier_a import run_tier_a
    from screening.tier_b import run_tier_b
    from screening.tier_c import run_tier_c

    if tier_a_results is None:
        tier_a_results = run_tier_a()
    if tier_b_results is None:
        # tier_a_results (вище) передається як готовий список тикерів,
        # щоб run_tier_b() не перераховував Tier A ще раз із нуля --
        # без цього повний прогін composite_score() рахував Tier A ДВІЧІ
        # (docs/decisions.md, 2026-09-25).
        tier_b_results = run_tier_b(tier_a_tickers=[r.ticker for r in tier_a_results])
    if tier_c_results is None:
        tier_c_results = run_tier_c(tier_a_results=tier_a_results, tier_b_results=tier_b_results)

    tier_a_by_ticker = {r.ticker: r for r in tier_a_results}
    tier_b_by_ticker = {r.ticker: r for r in tier_b_results}

    passed_c = [r for r in tier_c_results if r.passed]

    # Моментум — %-зміна ціни за MOMENTUM_DAYS торгових днів
    # (2026-10-13). Один batch-запит на всі тикери, не по одному:
    # passed_c це десятки тикерів, а `batch_series` уже вміє
    # віддавати кілька метрик одним SELECT.
    # Власне підключення: `run_composite_score()` історично працює
    # без conn (tier_a/tier_b/tier_c відкривають свої) — і тести
    # викликають її так само. Відкриваємо локально, щоб не міняти
    # підпис публічної функції.
    _momentum_conn = get_connection()
    try:
        momentum_by_ticker = _fetch_momentum(_momentum_conn, [c.ticker for c in passed_c])
    finally:
        _momentum_conn.close()

    rows = []
    skipped = []
    for c in passed_c:
        a = tier_a_by_ticker.get(c.ticker)
        b = tier_b_by_ticker.get(c.ticker)
        if a is None or b is None:
            skipped.append(c.ticker)
            continue

        # TierAResult.avg_dollar_volume і TierBResult.revenue_yoy/eps_yoy --
        # звичайні (не Optional) поля, завжди присутні. TierCResult.pe --
        # Optional[Decimal] типово, але для passed=True тикера завжди
        # обчислений (інакше він би не пройшов поріг MIN_PE..MAX_PE) --
        # перевірка залишена як страховка на випадок зміни tier_c.py.
        revenue_growth = b.revenue_yoy
        eps_growth = b.eps_yoy
        avg_dollar_volume = a.avg_dollar_volume
        pe = c.pe

        if pe is None:
            logger.warning("%s: pe відсутній у пройденого Tier C результату -- пропускаю", c.ticker)
            skipped.append(c.ticker)
            continue

        rows.append(
            {
                "ticker": c.ticker,
                "revenue_growth": revenue_growth,
                "eps_growth": eps_growth,
                "pe": pe,
                "avg_dollar_volume": avg_dollar_volume,
                # Тикер без достатньої історії цін отримує 0% — це
                # нейтральний моментум (не найгірший і не найкращий у
                # перцентилі), тобто відсутність даних не викидає
                # компанію зі скринінгу й не дає їй переваги.
                "momentum": momentum_by_ticker.get(c.ticker, Decimal("0")),
            }
        )

    if skipped:
        logger.warning("Пропущено через відсутні дані: %s", skipped)

    if not rows:
        return []

    revenue_pcts = percentile_ranks([r["revenue_growth"] for r in rows])
    eps_pcts = percentile_ranks([r["eps_growth"] for r in rows])
    neg_pe_pcts = percentile_ranks([-r["pe"] for r in rows])
    volume_pcts = percentile_ranks([r["avg_dollar_volume"] for r in rows])
    momentum_pcts = percentile_ranks([r["momentum"] for r in rows])

    results = []
    for row, rev_pct, eps_pct, negpe_pct, vol_pct, mom_pct in zip(
        rows, revenue_pcts, eps_pcts, neg_pe_pcts, volume_pcts, momentum_pcts
    ):
        score = (
            WEIGHT_REVENUE_GROWTH * rev_pct
            + WEIGHT_EPS_GROWTH * eps_pct
            + WEIGHT_NEG_PE * negpe_pct
            + WEIGHT_AVG_DOLLAR_VOLUME * vol_pct
            + WEIGHT_MOMENTUM * mom_pct
        )
        results.append(
            CompositeResult(
                ticker=row["ticker"],
                score=score,
                revenue_growth=row["revenue_growth"],
                eps_growth=row["eps_growth"],
                pe=row["pe"],
                avg_dollar_volume=row["avg_dollar_volume"],
                revenue_growth_pct=rev_pct,
                eps_growth_pct=eps_pct,
                neg_pe_pct=negpe_pct,
                avg_dollar_volume_pct=vol_pct,
                momentum_pct_change=row["momentum"],
                momentum_pct=mom_pct,
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results


def _fetch_price_context(conn, tickers: list[str]) -> dict[str, dict]:
    """{ticker: {"price_date", "change_pct", "volume_change_pct"}} --
    дешево: лише для тикерів, що вже пройшли Tier A->B->C (десятки, не
    весь S&P 500), той самий batch-принцип, що tier_a.py (N+1 фікс,
    docs/decisions.md 2026-09-25). `price_date` -- дата ОСТАННЬОГО
    закриття (не run_at самого скринінгу) -- живий фідбек користувача,
    2026-10-02: без цього неясно, на яку дату рахуються %-зміни.
    `volume_change_pct` -- % зміна обсягу торгів день-до-дня (той
    самий стиль, що `change_pct` для ціни; замінив $-обсяг на пряме
    прохання користувача того самого дня: "обсяг потрібно в
    відсотках зміни за 24 години"). Обидва None, якщо даних не
    вистачає (щойно зібраний тикер) -- caller робить .get()."""
    if not tickers:
        return {}

    close_ids = [f"{t.lower()}_close" for t in tickers]
    volume_ids = [f"{t.lower()}_volume" for t in tickers]
    # Межа дати (2026-10-16): дві останні торгові точки лежать у межах
    # пари тижнів, але свята дають розриви — беремо квартал із запасом.
    recent_window = calendar_days_for(2, SCREENING_PRICE_CALENDAR_BUFFER_DAYS) + 60
    closes_by_metric = batch_series(
        conn, "twelvedata", close_ids, limit_per_metric=2, max_age_days=recent_window
    )
    volumes_by_metric = batch_series(
        conn, "twelvedata", volume_ids, limit_per_metric=2, max_age_days=recent_window
    )

    context: dict[str, dict] = {}
    for ticker in tickers:
        close_series = closes_by_metric.get(f"{ticker.lower()}_close", [])
        if not close_series:
            continue
        latest_date, latest_close = close_series[0]
        entry = {"price_date": latest_date, "change_pct": None, "volume_change_pct": None}

        if len(close_series) >= 2:
            _, previous_close = close_series[1]
            if previous_close:
                entry["change_pct"] = (latest_close - previous_close) / previous_close * Decimal("100")

        volume_series = volumes_by_metric.get(f"{ticker.lower()}_volume", [])
        if len(volume_series) >= 2:
            _, latest_volume = volume_series[0]
            _, previous_volume = volume_series[1]
            if previous_volume:
                entry["volume_change_pct"] = (latest_volume - previous_volume) / previous_volume * Decimal("100")

        context[ticker] = entry
    return context


def _fetch_momentum(conn, tickers: list[str]) -> dict[str, Decimal]:
    """{ticker: %-зміна ціни за `MOMENTUM_DAYS` торгових днів}.

    Компонент скору з 2026-10-13: до цього 85% ваги composite score
    стояло на КВАРТАЛЬНИХ даних SEC, і скринінг 13 днів підряд
    віддавав той самий список із 14 тикерів у тому самому порядку
    (обґрунтування й живі числа — config.py, WEIGHT_MOMENTUM).

    Тикер без достатньої історії просто відсутній у результаті —
    caller підставляє 0% (нейтральний моментум), щоб брак даних не
    викидав компанію й не давав їй переваги."""
    if not tickers:
        return {}

    close_ids = [f"{t.lower()}_close" for t in tickers]
    # +1 точка: щоб порівняти ОСТАННЄ закриття з тим, що було
    # MOMENTUM_DAYS тому, потрібно MOMENTUM_DAYS+1 спостережень.
    series = batch_series(
        conn, "twelvedata", close_ids, limit_per_metric=MOMENTUM_DAYS + 1,
        max_age_days=calendar_days_for(
            MOMENTUM_DAYS + 1, SCREENING_PRICE_CALENDAR_BUFFER_DAYS
        ),
    )

    out: dict[str, Decimal] = {}
    for ticker in tickers:
        points = series.get(f"{ticker.lower()}_close", [])
        if len(points) < MOMENTUM_DAYS + 1:
            continue
        # batch_series віддає від найновішого до найстарішого.
        _, latest = points[0]
        _, oldest = points[MOMENTUM_DAYS]
        if not oldest:
            continue
        out[ticker] = (latest - oldest) / oldest * Decimal("100")
    return out


def _fetch_company_names(tickers: list[str]) -> dict[str, str]:
    """{ticker: назва компанії} з constituents.csv -- той самий
    мережевий запит, що вже використовує analysis/news_analysis/
    discovery/ (пошук новачків). Назва -- лише контекст виводу, не критерій
    скринінгу, тож мережевий збій тут НЕ повинен ламати весь прогін:
    порожній dict -- caller виводить тикер замість назви."""
    try:
        from collect_universe import fetch_sp500_constituents

        wanted = {t.upper() for t in tickers}
        return {
            c["symbol"].upper(): c["name"]
            for c in fetch_sp500_constituents()
            if c["symbol"].upper() in wanted
        }
    except Exception:
        logger.warning("Не вдалося отримати назви компаній (constituents.csv)", exc_info=True)
        return {}


def enrich_with_report_context(conn, results: list[CompositeResult]) -> None:
    """Проставляє company_name/price_change_24h_pct/price_date/
    volume_change_24h_pct на вже готовому ранжованому списку -- не впливає
    на score/порядок, лише контекст для screening_notify.py. Мутує
    results по місцю (той самий підхід, що інші enrich-style хелпери
    проєкту)."""
    if not results:
        return

    tickers = [r.ticker for r in results]
    names = _fetch_company_names(tickers)
    price_context = _fetch_price_context(conn, tickers)

    for r in results:
        r.company_name = names.get(r.ticker, r.ticker)
        ctx = price_context.get(r.ticker, {})
        r.price_change_24h_pct = ctx.get("change_pct")
        r.price_date = ctx.get("price_date")
        r.volume_change_24h_pct = ctx.get("volume_change_pct")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Composite score ранжування кандидатів")
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="Показати тільки топ-N (за замовчуванням -- усі, що пройшли Tier C)",
    )
    args = parser.parse_args()

    all_results = run_composite_score()

    logger.info("Ранжовано %d тикерів (усі, що пройшли Tier A -> B -> C)", len(all_results))

    conn = get_connection()
    try:
        enrich_with_report_context(conn, all_results)
        saved = save_screening_run(conn, all_results)
        logger.info("Збережено в screening_results: %d тикерів", saved)
    finally:
        conn.close()

    to_show = all_results[: args.top] if args.top else all_results

    print(f"{'#':<4}{'Ticker':<8}{'Score':>8}{'RevG%':>8}{'EpsG%':>8}{'P/E':>8}{'AvgVol$M':>10}")
    for i, r in enumerate(to_show, start=1):
        print(
            f"{i:<4}{r.ticker:<8}{float(r.score):>8.3f}"
            f"{float(r.revenue_growth) * 100:>7.1f}%"
            f"{float(r.eps_growth) * 100:>7.1f}%"
            f"{float(r.pe):>8.1f}"
            f"{float(r.avg_dollar_volume) / 1_000_000:>10.1f}"
        )
