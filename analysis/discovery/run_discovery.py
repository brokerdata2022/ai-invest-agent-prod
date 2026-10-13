#!/usr/bin/env python3
"""
Пошук новачків — активів, на які варто подивитись, бо з ними ЩОСЬ
ВІДБУВАЄТЬСЯ. Рішення користувача 2026-10-05: "сюди має попадати будь
який актив в якого є потенційне зростання або падіння, цей список має
оновлюватися автоматично".

Замінює `news_analysis/discover_candidates.py`, який за весь час дав
**0 рядків** (docs/archive/plan-journal-2026-10-11.md Фаза 5, docs/decisions.md 2026-10-05). Три
зміни по суті, не косметика:
1. **Три детектори замість одного.** Крипто-ф'ючерси (3 біржі, bulk),
   акції (ціни, що вже є в БД), тикери з новин. Один детектор = одна
   точка відмови, і вона й відмовила.
2. **Обидва напрямки.** Було "запропонуй перспективні акції" (лише
   зростання) — тепер |рух| в будь-яку сторону; падіння -18% це
   кандидат такого самого рівня, як зростання +18%.
3. **LLM більше не вигадує ідей.** Детерміновані детектори рахують
   рух/обсяг формулою (analysis/CLAUDE.md: не викликати LLM для того,
   що рахується кодом), а LLM лишився там, де справді потрібен —
   витягнути назви компаній із тексту новин (`news_tickers.py`).

Межа шарів (rule 1, CLAUDE.md): цей файл — лише проводка (БД/біржі →
детектори → відбір → БД). Пороги — `config.py` (розділ 7),
арифметика — `signals.py`, SQL — `_db.py`.

Використання:
    python run_discovery.py
    python run_discovery.py --dry-run       # порахувати й показати, не писати
    python run_discovery.py --no-news       # без LLM-детектора (безкоштовний прогін)
"""

import argparse
import logging
import os
import sys

import requests
from dotenv import load_dotenv

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Корінь репозиторію — для ЄДИНОГО `config.py` (рішення користувача
# 2026-10-04: один конфіг на весь агент).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))
sys.path.insert(0, _ANALYSIS_DIR)

from common.db import get_connection  # noqa: E402

from crypto.binance_futures_adapter import fetch_market_snapshot as fetch_binance_snapshot  # noqa: E402
from crypto.bybit_futures_adapter import fetch_market_snapshot as fetch_bybit_snapshot  # noqa: E402
from crypto.okx_futures_adapter import fetch_market_snapshot as fetch_okx_snapshot  # noqa: E402

import config  # noqa: E402
from crypto_screening.aggregate_sources import aggregate_snapshots  # noqa: E402
from llm_common import LlmError, call_llm, log_llm_call, require_api_key, resolve_provider  # noqa: E402
from news_analysis._db import fetch_relevant_for_aggregation  # noqa: E402
from news_analysis.aggregate import cluster_articles, top_clusters  # noqa: E402

from discovery import crypto_movers, news_tickers, stock_movers  # noqa: E402
from discovery._db import (  # noqa: E402
    fetch_company_names,
    fetch_crypto_volume_history,
    fetch_stock_series,
    fetch_tracked_identifiers,
    save_candidate,
)
from discovery.signals import select_top  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

def collect_crypto(conn, tracked: set[str]) -> list:
    """Bulk-знімок 3 бірж → агрегація → детектор. Відмова ОДНІЄЇ біржі
    не валить скан (той самий принцип, що
    `orchestration/jobs.py:_crypto_derivatives_collect`): лишаємось із
    даними решти, у лог іде попередження."""
    session = requests.Session()
    snapshots = {}
    for name, fetch in (
        ("binance_futures", fetch_binance_snapshot),
        ("bybit_futures", fetch_bybit_snapshot),
        ("okx_futures", fetch_okx_snapshot),
    ):
        try:
            snapshots[name] = fetch(session)
        except (requests.exceptions.RequestException, ValueError, KeyError):
            logger.warning("%s: знімок не отримано — скан без цієї біржі", name, exc_info=True)
            snapshots[name] = {}

    if not any(snapshots.values()):
        logger.error("Жодна з 3 бірж не віддала знімок — крипто-детектор пропущено")
        return []

    aggregated = aggregate_snapshots(
        snapshots["binance_futures"], snapshots["bybit_futures"], snapshots["okx_futures"]
    )
    volume_history = fetch_crypto_volume_history(conn, config.DISCOVERY_VOLUME_HISTORY_DAYS)
    return crypto_movers.detect(aggregated, volume_history, tracked)


def collect_stocks(conn, tracked: set[str]) -> list:
    series = fetch_stock_series(conn, config.DISCOVERY_STOCK_LOOKBACK_DAYS)
    names = fetch_company_names(conn, list(series))
    return stock_movers.detect(series, tracked, names)


def collect_news(conn, tracked: set[str], twelvedata_key: str) -> list:
    """LLM-екстракція тикерів із новин + детермінована верифікація.
    Аудит-лог виклику (rule 5, CLAUDE.md) пишеться тут, у проводці —
    тому сам `news_tickers.detect()` тестується без мережі."""
    require_api_key("candidate_discovery")

    # Потоки — переліком із конфіга, не один хардкоджений (живий
    # прогін 2026-10-05: `general` був порожній, працював лише
    # `geopolitical`). Кластеризація робиться ОДИН раз на зведений
    # набір: дедуплікація однієї історії, що прийшла і через GDELT, і
    # через RSS — це рівно те, для чого `cluster_articles` і є.
    articles: list[dict] = []
    for stream in config.DISCOVERY_NEWS_STREAMS:
        stream_articles = fetch_relevant_for_aggregation(
            conn, stream=stream, max_age_days=config.DISCOVERY_NEWS_MAX_AGE_DAYS
        )
        logger.info("потік %s: %d релевантних статей за вікно", stream, len(stream_articles))
        articles += stream_articles

    clusters = [
        c
        for c in cluster_articles(articles)
        if c.source_count >= config.DISCOVERY_NEWS_MIN_SOURCE_COUNT
    ]
    top = top_clusters(clusters, limit=config.DISCOVERY_NEWS_TOP_CLUSTERS)
    if not top:
        logger.info(
            "Немає історій із %d+ джерелами за %d дн у потоках %s — "
            "новинний детектор пропущено",
            config.DISCOVERY_NEWS_MIN_SOURCE_COUNT,
            config.DISCOVERY_NEWS_MAX_AGE_DAYS,
            ", ".join(config.DISCOVERY_NEWS_STREAMS),
        )
        return []

    prompt = news_tickers.build_prompt(top, sorted(tracked))
    try:
        llm = call_llm(prompt, news_tickers.SYSTEM_PROMPT, purpose="candidate_discovery")
        raw_content = llm.text
        extracted = news_tickers.parse_response(raw_content)
    except news_tickers.NewsTickerResponseError:
        logger.exception("Некоректна відповідь LLM — новинний детектор пропущено")
        return []
    except LlmError:
        logger.exception("Збій виклику LLM (обидва провайдери) — новинний детектор пропущено")
        return []

    llm_call_id = log_llm_call(
        conn,
        llm=llm,
        purpose="candidate_discovery",
        prompt=prompt,
        source_ref=None,
    )
    logger.info("LLM витягнув %d тикерів із %d історій", len(extracted), len(top))

    candidates = news_tickers.detect(conn, top, tracked, extracted, twelvedata_key)
    for candidate in candidates:
        candidate.llm_call_id = llm_call_id
    return candidates


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="порахувати й показати, не писати в БД")
    parser.add_argument("--no-news", action="store_true", help="без LLM-детектора новинних тикерів")
    parser.add_argument("--no-crypto", action="store_true", help="без крипто-детектора (без мережі)")
    args = parser.parse_args()

    conn = get_connection()
    try:
        tracked = fetch_tracked_identifiers(conn)
        logger.info("Уже відстежуємо %d ідентифікаторів — вони не можуть бути новачками", len(tracked))

        found: list = []
        if not args.no_crypto:
            found += collect_crypto(conn, tracked)
        found += collect_stocks(conn, tracked)

        if not args.no_news:
            twelvedata_key = os.environ.get("TWELVEDATA_API_KEY")
            if not twelvedata_key:
                logger.error("TWELVEDATA_API_KEY не задано — новинний детектор пропущено (див. .env.example)")
            else:
                found += collect_news(conn, tracked, twelvedata_key)

        selected = select_top(
            found,
            min_score=config.DISCOVERY_MIN_SCORE,
            max_items=config.DISCOVERY_MAX_ITEMS,
            max_per_detector=config.DISCOVERY_MAX_PER_DETECTOR,
        )
        logger.info("Знайдено %d кандидатів, відібрано %d", len(found), len(selected))

        arrow = {"up": "↑", "down": "↓"}
        for candidate in selected:
            logger.info(
                "%s %s [%s] score=%s — %s",
                arrow.get(candidate.direction, "·"),
                candidate.asset_id,
                candidate.detector,
                candidate.score,
                candidate.reason,
            )
            if not args.dry_run:
                save_candidate(conn, candidate)

        if args.dry_run:
            logger.info("--dry-run: у БД не записано")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
