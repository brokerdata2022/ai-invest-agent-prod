#!/usr/bin/env python3
"""
Звірка КОТИРУВАНЬ: акції (Twelve Data ↔ Yahoo) і крипто-спот
(Binance ↔ CoinGecko).

**Навіщо (2026-10-15, рішення користувача).** Принцип «правда — це
згода джерел» (2026-10-12) був реалізований лише для 11 watchlist-цін
і 4 макропоказників. А 503 тикери S&P ідуть з ОДНОГО неофіційного
джерела (Twelve Data), і на них стоять скринінг і список торгів;
крипто-спот — з одного Binance. Нагадування, чим це закінчується:
FRED давав Brent 135.51 проти справжніх 102.25 (33%), і
`freshness.py`/`quality.py` обидва казали «усе гаразд» — вони
відповідають про ЧАС, не про ПРАВИЛЬНІСТЬ.

**Що саме звіряється.** Не весь універсум: `QUOTES_CROSSCHECK_TICKERS`
найвищих за скором зі скринінгу — саме вони потрапляють у звіти, тож
хибна ціна там коштує найдорожче. Решта 470 тикерів читаються лише
масовою логікою, де одна помилкова ціна не стає рекомендацією.

**Чому НЕ переписує дані.** Критичне правило 6: сирі дані append-only.
Звірка лише КАЖЕ, що джерела розійшлись — рішення за людиною, бо
причини різні (корпоративна дія, інша дата зрізу, справжня помилка).
Те саме рішення, що в `crosscheck_macro.py`.

**Сповіщення — лише при розбіжності.** Звірка, що щодня звітує «усе
гаразд», стає шумом і її перестають читати (двічі перевірено на цьому
проєкті).

Використання:
    python crosscheck_quotes.py
    python crosscheck_quotes.py --notify
    python crosscheck_quotes.py --tickers 10
"""

import argparse
import logging
import os
import sys
from decimal import Decimal

import requests
from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_REPO_ROOT, "data-ingestion"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "reporting"))

from common.consensus import SourceValue, reconcile  # noqa: E402
from common.db import get_connection  # noqa: E402
from crypto.coingecko_adapter import PRICE_COIN_IDS, prices_by_date  # noqa: E402
from quotes.yahoo_chart_adapter import closes_by_date  # noqa: E402

import config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Звіряє котирування акцій (Twelve Data ↔ Yahoo) і крипто-спот "
    "(Binance ↔ CoinGecko); Telegram — лише при розбіжності."
)

AGREE = "agree"


def top_screening_tickers(conn, limit: int) -> list[str]:
    """Найвищі за скором тикери останнього прогону скринінгу."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ticker FROM screening_results
            WHERE run_at = (SELECT MAX(run_at) FROM screening_results)
            ORDER BY score DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [row[0] for row in cur.fetchall()]


def db_latest(conn, source: str, metric_id: str):
    """(дата, значення) останнього зрізу з нашої БД."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT observed_at, value FROM v_observations_latest_revision
            WHERE source = %s AND metric_id = %s
            ORDER BY observed_at DESC LIMIT 1
            """,
            (source, metric_id),
        )
        return cur.fetchone()


def crosscheck_stocks(conn, tickers: list[str], session) -> list[dict]:
    """Звіряє НАШ зріз із зрізом Yahoo ЗА ТУ САМУ ДАТУ.

    Живий дефект першої версії (2026-10-15): порівнювалось наше
    значення за 09.10 проти yahoo за 15.10, і розкид 2-5% виглядав як
    незгода джерел — а був застарілістю нашого зрізу. Застарілість —
    питання `common/freshness.py`, не звірки; змішувати їх означає
    отримати алерт, який нічого не означає."""
    results: list[dict] = []
    skipped_no_match = 0
    for ticker in tickers:
        ours = db_latest(conn, "twelvedata", f"{ticker.lower()}_close")
        if ours is None:
            continue
        series = closes_by_date(ticker, session=session)
        theirs = series.get(ours[0])
        if theirs is None:
            # Немає зрізу джерела на НАШУ дату — порівнювати нічого.
            skipped_no_match += 1
            continue
        verdict = reconcile(
            metric_id=ticker,
            values=[
                SourceValue(source="twelvedata", value=ours[1]),
                SourceValue(source="yahoo_chart", value=theirs),
            ],
            tolerance_pct=Decimal(str(config.QUOTES_CROSSCHECK_TOLERANCE_PCT)),
        )
        results.append({
            "asset": ticker,
            "kind": "акція",
            "ours": f"{ours[1]} ({ours[0]})",
            "theirs": f"{theirs} ({ours[0]})",
            "verdict": verdict,
        })
    if skipped_no_match:
        logger.info(
            "Пропущено %d тикерів: у Yahoo немає зрізу на нашу дату "
            "(наші дані застаріли або день неторговий)", skipped_no_match,
        )
    return results


def crosscheck_crypto(conn, session) -> list[dict]:
    results: list[dict] = []
    for asset_id in sorted(PRICE_COIN_IDS):
        ours = db_latest(conn, "binance", f"{asset_id}_close")
        if ours is None:
            continue
        series = prices_by_date(asset_id, session=session)
        theirs = series.get(ours[0])
        if theirs is None:
            logger.info(
                "%s: немає зрізу CoinGecko на нашу дату %s — не звіряю",
                asset_id, ours[0],
            )
            continue
        verdict = reconcile(
            metric_id=asset_id,
            values=[
                SourceValue(source="binance", value=ours[1]),
                SourceValue(source="coingecko", value=theirs),
            ],
            tolerance_pct=Decimal(str(config.CRYPTO_CROSSCHECK_TOLERANCE_PCT)),
        )
        results.append({
            "asset": asset_id,
            "kind": "крипта",
            "ours": f"{ours[1]} ({ours[0]})",
            "theirs": f"{theirs} ({ours[0]})",
            "verdict": verdict,
        })
    return results


def format_alert(mismatches: list[dict]) -> str:
    lines = ["⚠️ <b>Котирування: джерела розійшлись</b>", ""]
    for item in mismatches:
        spread = item["verdict"].max_spread_pct
        lines.append(
            f"• <b>{item['asset']}</b> ({item['kind']})\n"
            f"  наше: {item['ours']} | звірка: {item['theirs']}"
            + (f" | розкид {spread:.2f}%" if spread is not None else "")
        )
    lines.append("")
    lines.append(
        "Сирі дані не змінено (правило 6). Причини бувають різні: "
        "корпоративна дія, інша дата зрізу, справжня помилка джерела — "
        "але саме так знайшли Brent, завищений на 33%."
    )
    return "\n".join(lines)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--notify", action="store_true", help="Telegram ЛИШЕ при розбіжності")
    parser.add_argument(
        "--tickers", type=int, default=config.QUOTES_CROSSCHECK_TICKERS,
        help=f"скільки акцій звіряти (дефолт {config.QUOTES_CROSSCHECK_TICKERS})",
    )
    args = parser.parse_args()

    session = requests.Session()
    conn = get_connection()
    try:
        tickers = top_screening_tickers(conn, args.tickers)
        if not tickers:
            logger.warning(
                "Скринінг порожній — звіряти нічого. Це не збій звірки: "
                "спершу має пройти screening_composite_score."
            )
        results = crosscheck_stocks(conn, tickers, session) + crosscheck_crypto(conn, session)
    finally:
        conn.close()

    if not results:
        logger.info("Нічого не звірено — немає спільних активів у двох джерелах")
        return

    for item in results:
        verdict = item["verdict"]
        spread = verdict.max_spread_pct
        logger.info(
            "%s (%s): наше %s | звірка %s — %s%s",
            item["asset"], item["kind"], item["ours"], item["theirs"], verdict.status,
            f" (розкид {spread:.2f}%)" if spread is not None else "",
        )

    mismatches = [r for r in results if r["verdict"].status != AGREE]
    logger.info("Звірено %d активів, розбіжностей %d", len(results), len(mismatches))

    if not mismatches:
        logger.info(
            "Розбіжностей немає — сповіщення НЕ надсилається (звірка, що "
            "щодня звітує 'усе гаразд', стає шумом)"
        )
        return

    if args.notify:
        from _common import resolve_telegram_credentials  # noqa: E402
        from telegram_client import send_telegram_message  # noqa: E402

        token, chat_id = resolve_telegram_credentials()
        send_telegram_message(format_alert(mismatches), token=token, chat_id=chat_id)
        logger.info("Надіслано алерт про %d розбіжностей", len(mismatches))


if __name__ == "__main__":
    main()
