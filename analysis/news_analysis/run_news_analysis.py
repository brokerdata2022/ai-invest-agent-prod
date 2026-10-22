#!/usr/bin/env python3
"""
Ручний запуск DeepSeek-аналізу новин, зібраних у raw_news (news/
gdelt_adapter.py) і ще не проаналізованих.

Для стріму watchlist --tracked-assets за замовчуванням береться з
common/watchlist_db.py:fetch_asset_ids(conn) (2026-10-03 — раніше
news/queries.py:WATCHLIST_ASSET_IDS; те саме джерело істини, що й
query для GDELT-збору) — явний --tracked-assets перекриває це.

Використання:
    python run_news_analysis.py --stream watchlist --limit 20
    python run_news_analysis.py --tracked-assets AAPL,NVDA
"""

import argparse
import logging
import os
import re
import sys
from typing import Optional

from dotenv import load_dotenv
import requests

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Корінь репозиторію — для ЄДИНОГО `config.py` (рішення
# користувача 2026-10-04: один конфіг на весь агент).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _ANALYSIS_DIR)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))

from common.db import get_connection  # noqa: E402
from common.watchlist_db import fetch_asset_ids, fetch_patterns  # noqa: E402
from llm_common import LlmError, log_llm_call, require_api_key  # noqa: E402
from news_analysis._db import fetch_unanalyzed, save_analysis  # noqa: E402
from news_analysis.relevance_filter import analyze_article, DeepSeekResponseError  # noqa: E402
from screening._results_db import fetch_latest_tickers  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

def matched_watchlist_assets(title: str, patterns_by_asset: dict[str, tuple[str, ...]]) -> list[str]:
    """ДЕТЕРМІНОВАНИЙ пошук активів списку у заголовку статті.

    `patterns_by_asset` — `watchlist_assets.news_patterns`
    (`common/watchlist_db.py:fetch_patterns(conn, "news_patterns")`).
    Доти — `config.WATCHLIST_MATCH_TERMS` на 11 asset_id (живий замір
    і обґрунтування — docs/decisions.md 2026-10-12): актив із Telegram
    у пошук не потрапляв ніколи (2026-10-22).

    Це ПРЕФІЛЬТР, не призначення активу: повертає, про які активи
    стаття МОЖЕ бути, щоб `relevance_filter.py` поставив це питання
    DeepSeek. Остаточне рішення — за моделлю, тому хибний
    детермінований збіг ("gold mine air strikes" → xauusd) гине на
    LLM-кроці, а не потрапляє в БД."""
    lowered = (title or "").lower()
    return [
        asset_id
        for asset_id, patterns in patterns_by_asset.items()
        if any(re.search(pattern, lowered) for pattern in patterns)
    ]


def resolve_tracked_assets(
    article_stream: str,
    explicit: Optional[list[str]],
    screening_tickers: Optional[list[str]] = None,
    default_tracked_assets: Optional[dict[str, list[str]]] = None,
    title: Optional[str] = None,
    patterns_by_asset: Optional[dict[str, tuple[str, ...]]] = None,
) -> Optional[list[str]]:
    """Чиста функція вибору tracked_assets для однієї статті — явний
    --tracked-assets завжди виграє, інакше дефолт per stream
    (`default_tracked_assets` — watchlist і general його мають, рахує
    main() через `common/watchlist_db.py:fetch_asset_ids(conn)`,
    2026-10-03, живий редагований список, не хардкод) + тикери з
    останнього скринінгу (docs/decisions.md, "Ціль 2" — щоб DeepSeek
    міг проставити asset_id=тикер для акційних новин, не тільки для
    watchlist-активів).

    **`geopolitical` — через пошук, не через дефолт (2026-10-12).**
    Цей потік і далі НЕ отримує список активів цілком (вихідне
    заперечення 2026-10-06 чинне: "стаття про вибори — не новина про
    EUR/USD"), але якщо заголовок пройшов детермінований пошук
    (`matched_watchlist_assets`), стаття таки показується моделі — зі
    списком ТІЛЬКИ тих активів, які збіглись. Причина: 12 жовтня
    `news_collect_watchlist` (GDELT) провалив усі 4 прогони, потік
    watchlist дав 0 статей, а дві історії, що РЕАЛЬНО рухали Brent
    того дня (атаки в Ормузькій протоці), лежали в `geopolitical`
    невидимі для синтезу по активах. Тепер цей потік — резерв
    watchlist, якого доти не існувало."""
    if explicit:
        return explicit
    default = (default_tracked_assets or {}).get(article_stream)
    if default is None:
        matched = matched_watchlist_assets(title or "", patterns_by_asset or {})
        return matched or None
    if screening_tickers:
        return default + screening_tickers
    return default


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", choices=["watchlist", "general", "geopolitical"], default=None)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--tracked-assets", default=None,
        help="список через кому (напр. AAPL,NVDA) — перекриває дефолт per stream "
             "(watchlist: common/watchlist_db.py:fetch_asset_ids)",
    )
    args = parser.parse_args()

    # Ключ перевіряється через маршрут призначення, а не власним
    # читанням env: з 2026-10-05 провайдер для кожного виклику
    # обирається в `config.py:LLM_ROUTES`, і локальна перевірка саме
    # DEEPSEEK_API_KEY брехала б, якби маршрут перевели на Anthropic.
    require_api_key("news_relevance_filter")

    explicit_tracked_assets = args.tracked_assets.split(",") if args.tracked_assets else None

    conn = get_connection()
    try:
        screening_tickers = fetch_latest_tickers(conn)
        logger.info("Тикери з останнього скринінгу: %d", len(screening_tickers))

        # Дефолтні tracked_assets per stream, коли --tracked-assets не
        # задано явно.
        #
        # 2026-10-06: `general` теперь ТЕЖ отримує список
        # watchlist-активів (було — лише `watchlist`). Причина: GDELT
        # у тривалому штрафному стані, цільовий запит "лише про
        # золото" не працює, і без тегування статті ринкових RSS-фідів
        # (нафта, золото, BTC, курси) лягали з `asset_id=NULL` —
        # тобто синтез по активах (`synthesize.py`, читає ВСІ потоки)
        # їх не бачив. Питання до DeepSeek тут те саме, що для
        # watchlist-потоку ("чи стаття про один із цих активів?"), і
        # відповідь на нього не залежить від того, яким механізмом
        # статтю зібрано.
        #
        # `geopolitical` свідомо БЕЗ списку: це широкі редакційні
        # стрічки про події, не про активи, і тегування там дало б
        # хибні привʼязки (стаття про вибори — не новина про EUR/USD).
        watchlist_assets = fetch_asset_ids(conn)
        patterns_by_asset = fetch_patterns(conn, "news_patterns")
        default_tracked_assets = {
            "watchlist": watchlist_assets,
            "general": watchlist_assets,
        }

        articles = fetch_unanalyzed(conn, stream=args.stream, limit=args.limit)
        logger.info("Знайдено %d непроаналізованих статей", len(articles))

        analyzed = 0
        for article in articles:
            tracked_assets = resolve_tracked_assets(
                article["stream"], explicit_tracked_assets, screening_tickers,
                default_tracked_assets=default_tracked_assets,
                title=article.get("title"),
                patterns_by_asset=patterns_by_asset,
            )
            try:
                result, prompt, llm = analyze_article(
                    article, article["stream"], tracked_assets=tracked_assets
                )
            except DeepSeekResponseError:
                logger.exception("Пропускаю статтю %s: некоректна відповідь DeepSeek", article["url"])
                continue
            except LlmError:
                # Тимчасовий мережевий збій (DNS/timeout/5xx) на ОДНІЙ
                # статті не повинен губити решту вже готового пакету —
                # решта articles обробляються далі, цю можна повторити
                # наступним запуском (fetch_unanalyzed() підбере знову,
                # бо для неї ще нема рядка в news_analysis).
                logger.exception(
                    "Пропускаю статтю %s: мережева помилка виклику DeepSeek", article["url"]
                )
                continue

            llm_call_id = log_llm_call(
                conn,
                llm=llm,
                purpose="news_relevance_filter",
                prompt=prompt,
                source_ref=article["url"],
            )
            save_analysis(conn, article["id"], result, llm_call_id)
            analyzed += 1
            logger.info(
                "%s → relevant=%s asset_id=%s direction=%s",
                article["url"], result.is_relevant, result.asset_id, result.direction,
            )
    finally:
        conn.close()

    logger.info("Готово: %d статей проаналізовано", analyzed)


if __name__ == "__main__":
    main()
