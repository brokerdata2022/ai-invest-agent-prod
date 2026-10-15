#!/usr/bin/env python3
"""
ЖУРНАЛ НАСЛІДКІВ: що фактично сталося з тим, що агент порадив
подивитись.

**Постановка (рішення користувача 2026-10-15).** Агент видає списки
(`trading_list`), крипто-сетапи (LONG/SHORT/WATCH), новачків — і до
цього дня НІКОЛИ не перевіряв, що з них вийшло. Постфактум-оцінка
існувала лише для прогнозів показників (`backtest_verdicts`: ШІ проти
naive). Тобто всі ваги й пороги в `config.py` трималися на припущенні,
а проєкт уже тричі ловив вироджені виводи саме виміром, не оглядом
коду.

**Що робить.** Бере рішення, старші за `OUTCOME_HORIZON_DAYS`, дістає
ціну на дату рішення й ціну зараз, рахує фактичний рух і ставить
вердикт (`monitoring/outcomes.py`). Нічого не радить і не змінює в
рішеннях — лише фіксує факт.

**Чому це НЕ бектест стратегій** (він у PLAN.md «Поза MVP»). Бектест
прогнав би гіпотетичні угоди з входами, виходами й P&L. Тут немає ні
позицій, ні розміру, ні цін виконання — лише «агент сказав up, актив
пішов down». Це вимір якості ІНФОРМАЦІЇ, не торгівлі.

**Межі виміру, які треба знати, читаючи hit rate:**
- ціна береться на дату, а не на момент рішення (у БД денні зрізи);
- `flat` (рух менший за `OUTCOME_FLAT_BAND_PCT`) і `no_claim`
  (`watch`/`unclear` — не твердження про напрямок) у відсоток влучань
  не входять;
- `unavailable` — наша власна відсутність котирування, не помилка
  агента.

Використання:
    python evaluate_outcomes.py
    python evaluate_outcomes.py --dry-run     # порахувати й показати, не писати
"""

import argparse
import logging
import os
import sys
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_REPO_ROOT, "data-ingestion"))

from common.db import get_connection  # noqa: E402
from crypto.binance_futures_adapter import fetch_klines  # noqa: E402

import config  # noqa: E402
from _outcomes_db import (  # noqa: E402
    VENUE_BINANCE_FUTURES,
    VENUE_DB_STOCK,
    VENUE_DB_WATCHLIST,
    close_on_or_before,
    fetch_pending_decisions,
    save_outcome,
    watchlist_series,
)
from outcomes import UNAVAILABLE, realized_pct, verdict_for  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Оцінює постфактум рішення агента (список торгів, крипто-сетапи, новачки): "
    "фактичний рух активу за горизонт і вердикт correct/incorrect/flat."
)

# Скільки денних свічок тягнути з біржі. Горизонт пошуку рішень
# (OUTCOME_LOOKBACK_DAYS) плюс запас: klines повертає КАЛЕНДАРНІ дні для
# крипти (торгується 24/7), тож запас потрібен лише на пропуски біржі.
_KLINES_LIMIT = config.OUTCOME_LOOKBACK_DAYS + 10


def _klines_by_date(session, symbol: str) -> dict:
    """{дата: ціна закриття} для ф'ючерсного символу. Порожній дікт,
    якщо символ знято з лістингу — тоді вердикт буде `unavailable`, що
    чесніше за спробу вгадати ціну."""
    try:
        closes = fetch_klines(session, symbol, limit=_KLINES_LIMIT)
    except Exception:
        logger.warning("%s: klines недоступні", symbol, exc_info=True)
        return {}
    return {c["observed_at"]: c["value"] for c in closes}


def _price_from_series(series: dict, target: date, max_back_days: int = 7):
    """Ціна на дату або останню доступну перед нею (та сама межа 7
    днів, що в `close_on_or_before`: глибше — це вже не ціна «на цю
    дату»)."""
    for offset in range(max_back_days + 1):
        day = target - timedelta(days=offset)
        if day in series:
            return series[day]
    return None


def _resolve_db_prices(conn, decision: dict, eval_date: date):
    """Ціни з БД для акцій і watchlist-активів."""
    if decision["venue"] == VENUE_DB_STOCK:
        series = ("twelvedata", f"{decision['asset_key'].lower()}_close")
    else:
        row = watchlist_series(conn, decision["asset_key"])
        if row is None:
            return None, None, "немає у watchlist_assets"
        series = (row[0], row[1])

    decided_day = decision["decided_at"].date()
    start = close_on_or_before(conn, series[0], series[1], decided_day)
    end = close_on_or_before(conn, series[0], series[1], eval_date)
    if start is None or end is None:
        return None, None, f"немає котирування ({series[0]}/{series[1]})"
    # Та сама дата на обох кінцях = руху ще немає, міряти нічого.
    if start[0] == end[0]:
        return None, None, f"ціна лише за {start[0]} — руху за горизонт немає"
    return start[1], end[1], f"{series[0]}: {start[0]} → {end[0]}"


def evaluate(conn, dry_run: bool = False) -> list[dict]:
    """Оцінює всі зрілі рішення. Повертає записані (або пораховані)
    наслідки."""
    horizon = config.OUTCOME_HORIZON_DAYS
    decisions = fetch_pending_decisions(
        conn, lookback_days=config.OUTCOME_LOOKBACK_DAYS, horizon_days=horizon
    )
    logger.info("Рішень на оцінку: %d (горизонт %d днів)", len(decisions), horizon)
    if not decisions:
        return []

    session = requests.Session()
    # Один запит klines на СИМВОЛ, не на рішення: той самий символ
    # приходить і зі списку торгів, і з крипто-скринінгу, і з новачків.
    klines_cache: dict = {}
    results: list[dict] = []

    for decision in decisions:
        eval_date = min(date.today(), decision["decided_at"].date() + timedelta(days=horizon))

        if decision["venue"] == VENUE_BINANCE_FUTURES:
            symbol = decision["asset_key"].upper()
            if symbol not in klines_cache:
                klines_cache[symbol] = _klines_by_date(session, symbol)
            series = klines_cache[symbol]
            start = _price_from_series(series, decision["decided_at"].date())
            end = _price_from_series(series, eval_date)
            detail = f"binance klines: {len(series)} свічок"
            if start is None or end is None:
                start = end = None
                detail = "немає свічок на потрібні дати (делістинг?)"
        else:
            start, end, detail = _resolve_db_prices(conn, decision, eval_date)

        move = realized_pct(start, end)
        verdict = verdict_for(decision["direction"], move, config.OUTCOME_FLAT_BAND_PCT)

        outcome = {
            "decision_kind": decision["decision_kind"],
            "decision_id": decision["decision_id"],
            "asset_key": decision["asset_key"],
            "asset_class": decision["asset_class"],
            "direction": decision["direction"],
            "decided_at": decision["decided_at"],
            "horizon_days": horizon,
            "price_at_decision": start,
            "price_at_eval": end,
            "realized_pct": round(move, 4) if move is not None else None,
            "verdict": verdict,
            "detail": detail,
        }
        results.append(outcome)
        if not dry_run:
            save_outcome(conn, outcome)

    by_verdict: dict = {}
    for r in results:
        by_verdict[r["verdict"]] = by_verdict.get(r["verdict"], 0) + 1
    logger.info(
        "Оцінено %d: %s%s",
        len(results),
        ", ".join(f"{k}={v}" for k, v in sorted(by_verdict.items())),
        " (dry-run, не записано)" if dry_run else "",
    )
    unavailable = by_verdict.get(UNAVAILABLE, 0)
    if unavailable and unavailable == len(results):
        # Не падаємо, але кажемо вголос: 100% `unavailable` означає
        # зламану проводку цін, а не погані рішення агента.
        logger.warning(
            "ВСІ рішення — unavailable. Це ознака зламаного пошуку цін, "
            "не якості рішень: перевірте джерела котирувань."
        )
    return results


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--dry-run", action="store_true", help="порахувати й показати, не писати в БД")
    args = parser.parse_args()

    conn = get_connection()
    try:
        results = evaluate(conn, dry_run=args.dry_run)
        for r in sorted(results, key=lambda x: (x["decision_kind"], x["asset_key"]))[:40]:
            move = f"{r['realized_pct']:+.2f}%" if r["realized_pct"] is not None else "—"
            logger.info(
                "  %s %s (%s) -> %s %s",
                r["decision_kind"], r["asset_key"], r["direction"], move, r["verdict"],
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
