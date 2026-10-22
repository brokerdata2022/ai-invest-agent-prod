"""
Визначення профілю активу ЖИВИМИ запитами до джерел — спільне для
`/watchlist_add` (orchestration/telegram_commands.py) і разового
`backfill_watchlist_profile.py` (активи, додані до 2026-10-22).

Самі правила (клас за типом інструмента, терміни пошуку, символ
Yahoo) — чисті функції в `asset_profile.py`; тут лише мережа:
- тип і назва інструмента — Twelve Data `symbol_search` (без кредитів);
- CIK і назва компанії — довідник SEC (лише акції);
- друге джерело ціни — Yahoo, якщо символ підтвердився живою ціною.

Збій будь-якого з цих запитів не зупиняє додавання активу: актив без
CIK чи без другого джерела працює, просто біднішим — і це видно в
повідомленні (`/watchlist_add`) і в `/watchlist_list`.
"""

import logging
import os
from typing import Optional

import requests

from common import asset_profile
from companies.sec_edgar_adapter import lookup_company
from quotes.twelvedata_adapter import lookup_instrument
from quotes.yahoo_chart_adapter import YahooChartAdapter

logger = logging.getLogger(__name__)


def validate_yahoo_symbol(asset_id: str, symbol: str) -> bool:
    """Чи дає Yahoo ціну за цим символом — тоді він стає ДРУГИМ
    джерелом активу (звірка й резерв)."""
    try:
        return bool(YahooChartAdapter(metric_id=asset_id, symbol=symbol).collect(limit=5))
    except (ValueError, requests.exceptions.RequestException):
        logger.warning("Yahoo не підтвердив %r для %s", symbol, asset_id, exc_info=True)
        return False


def company_facts(asset_class: Optional[str], ticker: str) -> dict:
    """`{cik, company_name}` з SEC — лише для акцій; `{}`, якщо не акція,
    немає User-Agent або SEC не знає тикера."""
    if asset_class != "stock":
        return {}
    user_agent = os.environ.get("SEC_EDGAR_USER_AGENT")
    if not user_agent:
        logger.warning("SEC_EDGAR_USER_AGENT не задано — %s без CIK", ticker)
        return {}
    try:
        company = lookup_company(ticker, user_agent)
    except (requests.exceptions.RequestException, ValueError):
        logger.warning("SEC lookup провалився для %s", ticker, exc_info=True)
        return {}
    return {"cik": company["cik"], "company_name": company["title"]} if company else {}


def describe_twelvedata(ticker: str, api_key: Optional[str] = None) -> dict:
    """Що це за інструмент у Twelve Data: `{asset_class, commodity_group,
    name, cik?}`. `name` — назва компанії з SEC (для акцій), інакше
    назва інструмента з Twelve Data."""
    info = lookup_instrument(ticker, api_key) or {}
    asset_class, commodity_group = asset_profile.classify_instrument(
        info.get("instrument_type"), info.get("exchange"),
    )
    company = company_facts(asset_class, ticker)
    out = {
        "asset_class": asset_class,
        "commodity_group": commodity_group,
        "name": company.get("company_name") or info.get("instrument_name"),
    }
    if company.get("cik"):
        out["cik"] = company["cik"]
    return out


def stock_label(asset_class: Optional[str], ticker: str, name: Optional[str]) -> str:
    """'Skydance (SKYD)' для акції з відомою назвою, інакше тикер."""
    clean = asset_profile.clean_name(name)
    return f"{clean} ({ticker})" if asset_class == "stock" and clean else ticker


def build_profile(asset_id: str, asset_class: Optional[str], commodity_group: Optional[str],
                  ticker: str, name: Optional[str], primary: dict,
                  cik: Optional[str] = None) -> dict:
    """Колонки профілю `watchlist_assets` для нового активу: тип,
    терміни пошуку, ланцюжок джерел ціни (основне + Yahoo, якщо
    підтвердився)."""
    chain = [primary]
    symbol = asset_profile.yahoo_symbol(asset_class, ticker, asset_id)
    if symbol and validate_yahoo_symbol(asset_id, symbol):
        chain.append(asset_profile.chain_entry("yahoo_chart", asset_id, symbol))
    profile = {
        "asset_class": asset_class,
        "commodity_group": commodity_group,
        "company_name": name,
        "news_patterns": asset_profile.news_patterns(asset_class, asset_id, ticker, name),
        "ask_patterns": asset_profile.ask_patterns(asset_class, asset_id, ticker, name),
        "price_chain": chain,
    }
    if cik:
        profile["cik"] = cik
    return profile
