#!/usr/bin/env python3
"""
Збір цін списку обраних — по ЛАНЦЮЖКУ джерел КОЖНОГО активу
(`watchlist_assets.price_chain`), зі звіркою і вибором активного
джерела.

**Звідки це (2026-10-22).** Доти логіка жила в
`orchestration/jobs.py:_watchlist_prices` разом із хардкодом
`_FALLBACK_CHAINS` на 8 активів: актив, доданий через Telegram, резерву
не мав ніколи, а оркестрація містила бізнес-логіку всупереч своєму
CLAUDE.md. Тепер ланцюжок — у рядку активу, а цей модуль однаково
обробляє і початкові активи, і нові (`/watchlist_add` записує ланцюжок
сам: Twelve Data + Yahoo для акції/валюти).

**Порядок для активу з кількома джерелами** (не змінився з 2026-10-13):
1. зібрати з КОЖНОГО кандидата (повтор безпечний — append-only дедуп);
2. ЗВІРКА: викид виключається з вибору (`common/consensus.py`) —
   свіжість нічого не каже про правильність, усі хибні значення FRED
   по Brent були свіжі;
3. серед довірених — найсвіжіший (`choose_freshest_source`), і рядок
   перемикається на нього сам.

Крипту на Binance і CoinGecko щогодини збирає `crypto_prices`
(`orchestration/jobs.py`), тут вона пропускається — крім
`--include-hourly` (онбординг нового активу: потрібна ГЛИБОКА історія
одразу, а не за тиждень щогодинних прогонів).

Використання:
    python collect_watchlist.py                       # усі активи
    python collect_watchlist.py --asset skyd --limit 130 --include-hourly
"""

import argparse
import logging
import os
import sys
from typing import Optional

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from commodities.tradingeconomics_adapter import TradingEconomicsAdapter  # noqa: E402
from common.consensus import OUTLIER, SourceValue, reconcile  # noqa: E402
from common.db import get_connection, insert_observations  # noqa: E402
from common.watchlist_db import (  # noqa: E402
    SourceCandidate,
    choose_freshest_source,
    fetch_watchlist,
    price_chain,
)
from crypto.binance_adapter import BinanceAdapter  # noqa: E402
from macro.fred_adapter import FredAdapter  # noqa: E402
from quotes.twelvedata_adapter import TwelveDataAdapter  # noqa: E402
from quotes.yahoo_chart_adapter import SYMBOLS as YAHOO_SYMBOLS  # noqa: E402
from quotes.yahoo_chart_adapter import YahooChartAdapter  # noqa: E402

logger = logging.getLogger(__name__)

# Джерела, які щогодини збирає `crypto_prices` — тут лише за
# `include_hourly` (онбординг).
HOURLY_SOURCES = frozenset({"binance", "coingecko"})

# yahoo_chart — єдине джерело ГЛИБИНИ: тренд-шар вимагає
# `config.TREND_LONG_DAYS`=60 точок, тож Yahoo завжди просить не менше
# цього, незалежно від `limit` решти джерел.
_YAHOO_MIN_POINTS = 130


def _base_metric(metric_id: str) -> str:
    """'skyd_close' → 'skyd': адаптери Twelve Data і Binance самі
    дописують суфікс поля (`_close`/`_volume`), тож їм передається
    база, а в ланцюжку лежить УЖЕ збережений id."""
    return metric_id[: -len("_close")] if metric_id.endswith("_close") else metric_id


def collect_candidate(candidate: SourceCandidate, limit: int, include_hourly: bool = False) -> Optional[list]:
    """Записи одного кандидата або None — "не збирається тут" (щогодинне
    джерело, ручний запис, немає ключа)."""
    source = candidate.source
    if source == "yahoo_chart":
        # Для початкових активів символ — зі словника адаптера, а НЕ з
        # `ticker`: старий `set_source` при перемиканні на Yahoo лишав
        # у рядку попередній ticker (слаг TradingEconomics "gold"), і
        # передати його як символ Yahoo означало б запитати не той
        # інструмент. Нові активи в словнику відсутні — їм символ дає
        # ланцюжок.
        symbol = None if candidate.metric_id in YAHOO_SYMBOLS else candidate.ticker
        return YahooChartAdapter(metric_id=candidate.metric_id, symbol=symbol).collect(
            limit=max(limit, _YAHOO_MIN_POINTS)
        )
    if source == "tradingeconomics":
        return TradingEconomicsAdapter(metric_id=candidate.metric_id, slug=candidate.ticker).collect()
    if source == "twelvedata":
        api_key = os.environ.get("TWELVEDATA_API_KEY")
        if not api_key or not candidate.ticker:
            logger.error("%s: TWELVEDATA_API_KEY чи тикер не задано — пропущено", candidate.metric_id)
            return None
        return TwelveDataAdapter(
            api_key=api_key, ticker=candidate.ticker, metric_id=_base_metric(candidate.metric_id),
        ).collect(limit=limit)
    if source == "fred":
        api_key = os.environ.get("FRED_API_KEY")
        if not api_key:
            logger.error("%s: FRED_API_KEY не задано — пропущено", candidate.metric_id)
            return None
        return FredAdapter(api_key=api_key, metric_id=candidate.metric_id).collect(limit=limit)
    if source == "binance":
        if not include_hourly:
            return None
        return BinanceAdapter(metric_id=_base_metric(candidate.metric_id), symbol=candidate.ticker).collect(limit=limit)
    if source in HOURLY_SOURCES or source == "web_crosscheck":
        # coingecko — щогодини; web_crosscheck — ручний запис
        # (`common/manual_observation.py`), збирати нічого.
        return None
    logger.warning("%s: невідоме джерело %r — пропущено", candidate.metric_id, source)
    return None


def tolerance_for(asset: dict):
    """Допуск звірки за КЛАСОМ активу (`config.CONSENSUS_TOLERANCE_PCT`)
    — доти за asset_id, і новий актив завжди падав на дефолт."""
    table = config.CONSENSUS_TOLERANCE_PCT
    return table.get(asset.get("asset_class") or "", table["default"])


def exclude_outliers(conn, asset: dict, chain: list[SourceCandidate]) -> list[SourceCandidate]:
    """Кандидати без тих, чиє останнє значення розходиться з рештою
    понад допуск. Немає викиду, менше двох джерел або згоди взагалі
    немає (`disagree` — переможця нічим визначити) — ланцюжок як є:
    завдання вузьке — не дати ВИКИДУ стати активним джерелом."""
    values = []
    with conn.cursor() as cur:
        for candidate in chain:
            cur.execute(
                """
                SELECT value FROM raw_observations
                WHERE source = %s AND metric_id = %s
                ORDER BY observed_at DESC, revision DESC
                LIMIT 1
                """,
                (candidate.source, candidate.metric_id),
            )
            row = cur.fetchone()
            if row is not None:
                values.append(SourceValue(candidate.source, row[0]))

    verdict = reconcile(asset["asset_id"], values, tolerance_for(asset))
    if verdict.status != OUTLIER:
        return chain
    logger.warning(
        "%s: звірка джерел — %s; викид(и) виключено з вибору активного джерела",
        asset["asset_id"], verdict.detail,
    )
    remaining = [c for c in chain if c.source not in verdict.outliers]
    return remaining or chain


def collect_asset(conn, asset: dict, limit: int = 10, include_hourly: bool = False) -> SourceCandidate:
    """Зібрати всі джерела одного активу й повернути активне."""
    asset_id = asset["asset_id"]
    chain = price_chain(asset)
    for candidate in chain:
        try:
            records = collect_candidate(candidate, limit, include_hourly=include_hourly)
            if records:
                inserted = insert_observations(conn, records)
                logger.info(
                    "%s: %s — %d записів (%d нових/змінених)",
                    asset_id, candidate.source, len(records), inserted,
                )
        except Exception:
            logger.error("%s: джерело %s провалилось", asset_id, candidate.source, exc_info=True)
            conn.rollback()

    if len(chain) < 2:
        return chain[0]
    trusted = exclude_outliers(conn, asset, chain)
    chosen = choose_freshest_source(conn, asset_id, trusted)
    logger.info("%s: активне джерело -> %s", asset_id, chosen.source)
    return chosen


def collect_watchlist_prices(conn, asset_ids: Optional[set] = None, limit: int = 10,
                             include_hourly: bool = False) -> int:
    """Усі (чи вибрані) увімкнені активи. Один актив не валить решту.
    Повертає, скільки активів оброблено."""
    processed = 0
    for asset in fetch_watchlist(conn):
        if asset_ids and asset["asset_id"] not in asset_ids:
            continue
        try:
            collect_asset(conn, asset, limit=limit, include_hourly=include_hourly)
            processed += 1
        except Exception:
            logger.error("%s: збір провалився", asset["asset_id"], exc_info=True)
            conn.rollback()
    return processed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", action="append", default=None, help="asset_id (можна кілька)")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--include-hourly", action="store_true",
                        help="збирати й Binance (онбординг: глибока історія одразу)")
    args = parser.parse_args()

    conn = get_connection()
    try:
        processed = collect_watchlist_prices(
            conn, set(args.asset) if args.asset else None,
            limit=args.limit, include_hourly=args.include_hourly,
        )
    finally:
        conn.close()
    if args.asset and processed == 0:
        sys.exit(f"Жодного з {args.asset} немає серед увімкнених активів")
    logger.info("Готово: оброблено %d активів", processed)


if __name__ == "__main__":
    main()
