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
from datetime import date

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

import change_gate  # noqa: E402
import config  # noqa: E402
from fundamental._db import (  # noqa: E402
    fetch_fundamental_series,
    fetch_latest_analyses,
    fetch_latest_filing_insight,
    fetch_top_stocks,
    fetch_watchlist_assets,
    save_fundamental_analysis,
    save_verdict,
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
from fundamental.gate_inputs import asset_gate_inputs, stock_gate_inputs  # noqa: E402
from fundamental.verdict import (  # noqa: E402
    SYSTEM_PROMPT as VERDICT_SYSTEM_PROMPT,
    build_verdict_prompt,
    parse_verdict_response,
)

# Що повертає аналіз одного активу/акції: новий висновок збережено,
# входи не змінились (гейт змін — висновок минулого прогону лишається
# чинним), або пропущено (замало даних / збій).
ANALYZED = "analyzed"
UNCHANGED = "unchanged"
SKIPPED = "skipped"

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


def analyze_stock(conn, stock: dict, force: bool = False) -> str:
    """ANALYZED — аналіз збережено; UNCHANGED — входи ті самі, що в
    минулого аналізу (гейт змін); SKIPPED — замало даних. Причина — у
    лозі. `force` — оминути гейт (ручний прогін одного тикера)."""
    ticker = stock["ticker"]
    series, revenue_points = collect_series(conn, ticker)

    if revenue_points < config.MIN_QUARTERS:
        logger.warning(
            "%s: лише %d квартальних точок виручки (потрібно >= %d) — пропущено",
            ticker, revenue_points, config.MIN_QUARTERS,
        )
        return SKIPPED

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
    stable, anchors = stock_gate_inputs(series, valuation, filing_insight)
    decision = change_gate.check(conn, "fundamental_analysis", ticker, stable, anchors)
    if not decision.changed and not force:
        logger.info("%s: %s (з %s) — висновок лишається чинним", ticker, decision.reason, decision.since)
        return UNCHANGED
    logger.info("%s: %s — аналізуємо", ticker, decision.reason)

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
    change_gate.remember(conn, "fundamental_analysis", ticker, stable, anchors)
    logger.info(
        "%s: direction=%s confidence=%.2f (сильних %d, ризиків %d): %s",
        ticker, result.direction, result.confidence,
        len(result.strengths), len(result.risks), result.summary,
    )
    return ANALYZED


def analyze_watchlist_asset(
    conn, asset: dict, watchlist: list[dict], dry_run: bool = False, force: bool = False,
) -> str:
    """Розгорнутий аналіз активу СПИСКУ ОБРАНИХ за ЙОГО чинниками
    (рішення користувача 2026-10-04). На відміну від акцій, вхід — не
    звітність, а власний для кожного активу набір чинників: ручний із
    `config.py:ASSET_FACTORS` або складений ШІ для активу, доданого
    через Telegram (`fundamental/checklist.py`, 2026-10-20).

    `dry_run` — друкує промпт без жодного виклику ШІ (і чек-лист не
    складається наново — береться збережений). `force` — оминути гейт
    змін (ручний прогін одного активу, онбординг)."""
    asset_id = asset["asset_id"]
    checklist = resolve_checklist(conn, asset, watchlist, allow_generate=not dry_run)
    asset_factors = collect_asset_factors(
        conn,
        asset_id=asset_id,
        label=asset.get("label") or asset_id,
        price_source=asset["source"],
        price_metric_id=asset["metric_id"],
        checklist=checklist,
        # Для акції списку — блок компанії (звітність, тези зі звіту)
        # читається за тикером (2026-10-22).
        ticker=asset.get("ticker") if asset.get("asset_class") == "stock" else None,
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
        return SKIPPED

    stable, anchors = asset_gate_inputs(asset_factors, checklist.origin)
    decision = change_gate.check(conn, "fundamental_analysis_asset", asset_id, stable, anchors)

    prompt = build_asset_prompt(asset_factors)
    if dry_run:
        print(
            f"\n===== {asset_id} ({checklist.origin}) — гейт: "
            f"{'змінилось' if decision.changed else 'без змін'}, {decision.reason} =====\n{prompt}\n"
        )
        return ANALYZED if decision.changed else UNCHANGED

    if not decision.changed and not force:
        logger.info("%s: %s (з %s) — висновок лишається чинним", asset_id, decision.reason, decision.since)
        return UNCHANGED
    logger.info("%s: %s — аналізуємо", asset_id, decision.reason)

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
    change_gate.remember(conn, "fundamental_analysis_asset", asset_id, stable, anchors)
    logger.info(
        "%s: direction=%s confidence=%.2f (чинників %d із %d, сильних %d, "
        "ризиків %d): %s",
        asset_id, result.direction, result.confidence,
        len(asset_factors.available), len(asset_factors.factors),
        len(result.strengths), len(result.risks), result.summary,
    )
    return ANALYZED


def run_verdict(conn, asset_ids: list[str], today=None) -> bool:
    """ФІНАЛЬНИЙ ВЕРДИКТ (2026-10-24): один виклик Anthropic поверх
    останніх висновків по всіх активах прогону. True — збережено.

    Вхід — останній висновок КОЖНОГО активу, не лише сьогоднішній:
    актив без змін входів сьогодні нового рядка не має, але у вердикті
    бути мусить (з позначкою "без змін з <дата>")."""
    today = today or date.today()
    analyses = fetch_latest_analyses(conn, asset_ids, config.CHANGE_GATE_MAX_AGE_DAYS)
    if not analyses:
        logger.warning("Вердикт: немає жодного висновку по активах — пропущено")
        return False

    prompt = build_verdict_prompt(analyses, today)
    try:
        llm = call_llm(prompt, VERDICT_SYSTEM_PROMPT, purpose="fundamental_verdict")
    except LlmError:
        # Без вердикту notify надішле висновки по активах як раніше —
        # окремими повідомленнями, тож інформація не губиться.
        logger.exception("Вердикт: збій виклику LLM — пропущено")
        return False
    llm_call_id = log_llm_call(
        conn, llm=llm, purpose="fundamental_verdict", prompt=prompt, source_ref=str(today),
    )
    try:
        result = parse_verdict_response(llm.text)
    except SynthesisResponseError:
        logger.exception("Вердикт: некоректна відповідь LLM — пропущено")
        return False
    save_verdict(conn, today, result, [row["id"] for row in analyses], llm_call_id)
    logger.info(
        "Вердикт по %d активах: %s (ключових %d, суперечностей %d)",
        len(analyses), result.summary[:160], len(result.key_points), len(result.conflicts),
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
    parser.add_argument(
        "--force", action="store_true",
        help="аналізувати навіть без змін входів (оминути гейт змін)",
    )
    args = parser.parse_args()
    # Ручний прогін одного активу/тикера — завжди свіжий аналіз: людина
    # просить подивитись зараз, а не "те саме, що вчора".
    force = args.force or bool(args.asset or args.ticker)

    if not args.dry_run:
        require_api_key("fundamental_analysis")

    conn = get_connection()
    try:
        analyzed = unchanged = total = 0
        verdict_assets: list[str] = []

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
                    status = analyze_watchlist_asset(
                        conn, asset, watchlist, dry_run=args.dry_run, force=force,
                    )
                    if status != SKIPPED:
                        verdict_assets.append(asset["asset_id"])
                    analyzed += status == ANALYZED
                    unchanged += status == UNCHANGED
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
                        status = analyze_stock(conn, stock, force=force)
                        if status != SKIPPED:
                            verdict_assets.append(stock["ticker"])
                        analyzed += status == ANALYZED
                        unchanged += status == UNCHANGED
                    except SynthesisResponseError:
                        logger.exception("%s: некоректна відповідь LLM", stock["ticker"])
                        conn.rollback()
                    except LlmError:
                        logger.exception("%s: збій виклику LLM", stock["ticker"])
                        conn.rollback()
                    except Exception:
                        logger.exception("%s: аналіз провалився", stock["ticker"])
                        conn.rollback()

        logger.info(
            "Готово: проаналізовано %d, без змін входів %d, усього %d",
            analyzed, unchanged, total,
        )

        # Фінальний вердикт — лише для повного прогону (не для одного
        # активу/тикера чи половини) і лише коли є щось нове: якщо
        # жоден висновок не змінився, вчорашній вердикт лишається
        # чинним, і в Telegram не йде те саме іншими словами.
        full_run = not (args.asset or args.ticker or args.only or args.dry_run)
        if full_run and analyzed:
            run_verdict(conn, verdict_assets)
        elif full_run:
            logger.info("Вердикт: жоден висновок не змінився — пропущено")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
