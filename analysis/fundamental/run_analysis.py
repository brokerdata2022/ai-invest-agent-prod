#!/usr/bin/env python3
"""
Фундаментальний LLM-аналіз акцій (спек користувача 2026-09-28:
"Фундаментальний ШІ-аналіз о 14:00 кожного робочого дня для активів зі
списку обраних") — наскрізний шлях "звітність із БД → LLM →
fundamental_analysis".

Межа покриття, названа честно: у спеку сказано "watchlist + пройшли
скринінг", але у watchlist-активів (золото, нафта, газ, BTC, валютні
пари) фундаменталу НЕ ІСНУЄ — немає ні виручки, ні EPS, ні P/E. Тому
аналізуються лише АКЦІЇ (sec_edgar + screening_results); вдавати, що
товар має фундаментал, було б гірше за чесну межу (db/schema.sql).

Межа шарів (rule 1): цей файл — лише проводка. Промпт і розбір
відповіді — `fundamental_llm.py`, пороги/ліміти — `config.py`, SQL —
`_db.py`.

Використання:
    python run_analysis.py
    python run_analysis.py --top 3
    python run_analysis.py --ticker HPE     # один тикер, для перевірки
    python run_analysis.py --asset xauusd --dry-run   # промпт без виклику ШІ
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
import requests

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Корінь репозиторію — для ЄДИНОГО `config.py` (рішення
# користувача 2026-10-04: один конфіг на весь агент).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))
sys.path.insert(0, _ANALYSIS_DIR)

from common.db import get_connection  # noqa: E402
from llm_common import (  # noqa: E402
    LlmError,
    SynthesisResponseError,
    call_llm,
    log_llm_call,
    require_api_key,
)

import config  # noqa: E402
from fundamental._db import (  # noqa: E402
    fetch_fundamental_series,
    fetch_latest_filing_insight,
    fetch_top_stocks,
    fetch_watchlist_assets,
    save_fundamental_analysis,
)
from fundamental.checklist import ORIGIN_DEFAULT, resolve_checklist  # noqa: E402
from fundamental.factors import collect_asset_factors  # noqa: E402
from fundamental.fundamental_llm import (  # noqa: E402
    ASSET_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_asset_prompt,
    build_prompt,
    parse_fundamental_response,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def collect_series(conn, ticker: str) -> tuple[dict, int]:
    """({людська назва: точки}, скільки квартальних точок по виручці) —
    кількість по виручці окремо, бо саме вона вирішує, чи є про що
    говорити (`config.MIN_QUARTERS`)."""
    series: dict[str, list[dict]] = {}
    revenue_points = 0
    for suffix, label in config.FUNDAMENTAL_METRICS:
        points = fetch_fundamental_series(conn, ticker, suffix, config.QUARTERS)
        if points:
            series[label] = points
        if suffix == "revenue":
            revenue_points = len(points)
    return series, revenue_points


def analyze_stock(conn, stock: dict) -> bool:
    """True — аналіз збережено. False — тикер пропущено (замало даних
    або збійна відповідь LLM); причина в лозі."""
    ticker = stock["ticker"]
    series, revenue_points = collect_series(conn, ticker)

    if revenue_points < config.MIN_QUARTERS:
        logger.warning(
            "%s: лише %d квартальних точок виручки (потрібно >= %d) — пропущено",
            ticker, revenue_points, config.MIN_QUARTERS,
        )
        return False

    valuation = {
        "pe": stock.get("pe"),
        "revenue_growth": stock.get("revenue_growth"),
        "eps_growth": stock.get("eps_growth"),
    }
    # Тези з ТЕКСТУ звіту — те, чого в числах немає (2026-10-13).
    # None, якщо `filing_insights` для тикера ще не прогонявся: аналіз
    # тоді працює як раніше, лише без причин.
    filing_insight = fetch_latest_filing_insight(conn, ticker)
    if filing_insight:
        logger.info(
            "%s: у промпт додано тези зі звіту %s %s",
            ticker, filing_insight["form"], filing_insight["filing_date"],
        )
    prompt = build_prompt(
        ticker, stock.get("company_name"), series, valuation,
        filing_insight=filing_insight,
    )
    llm = call_llm(prompt, SYSTEM_PROMPT, purpose="fundamental_analysis")
    raw_content = llm.text

    # Аудит-лог ДО розбору (rule 5): збійну відповідь найцікавіше
    # розбирати постфактум, і без запису від неї не лишилось би сліду.
    llm_call_id = log_llm_call(
        conn,
        llm=llm,
        purpose="fundamental_analysis",
        prompt=prompt,
        source_ref=ticker,
    )

    result = parse_fundamental_response(raw_content)
    save_fundamental_analysis(
        conn,
        asset_id=ticker,
        asset_kind="stock",
        ticker=ticker,
        company_name=stock.get("company_name"),
        result=result,
        inputs={"valuation": valuation, "series": series},
        llm_call_id=llm_call_id,
    )
    logger.info(
        "%s: direction=%s confidence=%.2f (сильних %d, ризиків %d): %s",
        ticker, result.direction, result.confidence,
        len(result.strengths), len(result.risks), result.summary,
    )
    return True


def analyze_watchlist_asset(conn, asset: dict, watchlist: list[dict], dry_run: bool = False) -> bool:
    """Розгорнутий аналіз активу СПИСКУ ОБРАНИХ за ЙОГО чинниками
    (рішення користувача 2026-10-04). На відміну від акцій, вхід — не
    звітність, а власний для кожного активу набір чинників: ручний із
    `config.py:ASSET_FACTORS` або складений ШІ для активу, доданого
    через Telegram (`fundamental/checklist.py`, 2026-10-20).

    `dry_run` — друкує промпт без жодного виклику ШІ (і чек-лист не
    складається наново — береться збережений)."""
    asset_id = asset["asset_id"]
    checklist = resolve_checklist(conn, asset, watchlist, allow_generate=not dry_run)
    asset_factors = collect_asset_factors(
        conn,
        asset_id=asset_id,
        label=asset.get("label") or asset_id,
        price_source=asset["source"],
        price_metric_id=asset["metric_id"],
        checklist=checklist,
    )

    logger.info("%s: чек-лист — %s (%d чинників)", asset_id, checklist.origin, len(checklist.specs))
    if checklist.origin == ORIGIN_DEFAULT:
        logger.warning(
            "%s: власного чек-листа немає — універсальний набір "
            "(config.py:DEFAULT_ASSET_FACTORS)", asset_id,
        )

    if not asset_factors.available:
        logger.warning(
            "%s: жоден чинник не має даних — пропущено (аналіз без входів "
            "був би вигадкою)", asset_id,
        )
        return False

    prompt = build_asset_prompt(asset_factors)
    if dry_run:
        print(f"\n===== {asset_id} ({checklist.origin}) =====\n{prompt}\n")
        return True

    llm = call_llm(prompt, ASSET_SYSTEM_PROMPT, purpose="fundamental_analysis_asset")
    raw_content = llm.text

    llm_call_id = log_llm_call(
        conn,
        llm=llm,
        purpose="fundamental_analysis_asset",
        prompt=prompt,
        source_ref=asset_id,
    )

    result = parse_fundamental_response(raw_content)
    save_fundamental_analysis(
        conn,
        asset_id=asset_id,
        asset_kind="watchlist",
        company_name=asset.get("label"),
        result=result,
        inputs={
            "factors": [
                {"label": f.label, "points": f.points, "texts": f.texts}
                for f in asset_factors.available
            ],
            "unavailable": [
                {"label": f.label, "reason": f.missing_reason}
                for f in asset_factors.unavailable
            ],
            "gaps": list(asset_factors.gaps),
            "checklist_origin": checklist.origin,
            "trust_notes": {f.label: f.note for f in asset_factors.available if f.note},
        },
        llm_call_id=llm_call_id,
    )
    logger.info(
        "%s: direction=%s confidence=%.2f (чинників %d із %d, сильних %d, "
        "ризиків %d): %s",
        asset_id, result.direction, result.confidence,
        len(asset_factors.available), len(asset_factors.factors),
        len(result.strengths), len(result.risks), result.summary,
    )
    return True


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=config.TOP_N_STOCKS)
    parser.add_argument(
        "--ticker", default=None,
        help="проаналізувати ОДИН тикер замість топу скринінгу (перевірка)",
    )
    parser.add_argument(
        "--asset", default=None,
        help="проаналізувати ОДИН актив watchlist за його чинниками (перевірка)",
    )
    parser.add_argument(
        "--only", choices=("watchlist", "stocks"), default=None,
        help="обмежити прогін однією половиною (за замовчуванням — обидві)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="активи watchlist: надрукувати промпт аналізу без жодного виклику ШІ "
             "(чек-лист береться збережений, наново не складається)",
    )
    args = parser.parse_args()

    if not args.dry_run:
        require_api_key("fundamental_analysis")

    conn = get_connection()
    try:
        analyzed = total = 0

        # --- активи СПИСКУ ОБРАНИХ (кожен за своїми чинниками) ---
        if args.only != "stocks":
            watchlist = fetch_watchlist_assets(conn)
            if args.asset:
                assets = [a for a in watchlist if a["asset_id"] == args.asset]
                if not assets:
                    logger.error("%s: немає в watchlist", args.asset)
            else:
                assets = watchlist

            logger.info("Аналізуємо %d активів зі списку обраних", len(assets))
            for asset in assets:
                total += 1
                try:
                    if analyze_watchlist_asset(conn, asset, watchlist, dry_run=args.dry_run):
                        analyzed += 1
                except SynthesisResponseError:
                    logger.exception("%s: некоректна відповідь LLM", asset["asset_id"])
                    conn.rollback()
                except LlmError:
                    logger.exception("%s: збій виклику LLM", asset["asset_id"])
                    conn.rollback()
                except Exception:
                    # Один актив не валить цикл — той самий патерн, що
                    # update_forecasts.py/compare_releases.py.
                    logger.exception("%s: аналіз провалився", asset["asset_id"])
                    conn.rollback()

        # --- акції зі скринінгу (вхід — звітність, не чинники) ---
        if args.only != "watchlist" and not args.asset and not args.dry_run:
            if args.ticker:
                stocks = [{"ticker": args.ticker.upper(), "company_name": None}]
            else:
                stocks = fetch_top_stocks(conn, args.top)

            if not stocks:
                logger.warning(
                    "Скринінг не дав жодного тикера — спершу потрібен прогін "
                    "analysis/screening/composite_score.py"
                )
            else:
                logger.info("Аналізуємо %d акцій", len(stocks))
                for stock in stocks:
                    total += 1
                    try:
                        if analyze_stock(conn, stock):
                            analyzed += 1
                    except SynthesisResponseError:
                        logger.exception("%s: некоректна відповідь LLM", stock["ticker"])
                        conn.rollback()
                    except LlmError:
                        logger.exception("%s: збій виклику LLM", stock["ticker"])
                        conn.rollback()
                    except Exception:
                        logger.exception("%s: аналіз провалився", stock["ticker"])
                        conn.rollback()

        logger.info("Готово: проаналізовано %d з %d", analyzed, total)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
