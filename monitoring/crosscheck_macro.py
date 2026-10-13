#!/usr/bin/env python3
"""
Звірка макропоказників США між FRED (дзеркало) і BLS (першоджерело).

**Навіщо (2026-10-13).** Принцип користувача: "корекні дані це онлайн
пошук і звірка на декількох джерелах". Для watchlist-цін звірка вже
працює й одразу зловила реальне: FRED давав Brent 135.51 проти
справжніх 102.25 (33%). Після того випадку "з макро, напевно, все
гаразд" — припущення, не знання, а макро — це вхід у ПРОГНОЗИ, тобто
хибне значення там коштує дорожче за хибну ціну в звіті.

**Що саме робить.** Бере останнє спільне спостереження по кожному
показнику з обох джерел і проганяє через `common/consensus.py`.
Результат — у лог і в Telegram, АЛЕ лише коли є розбіжність:
звірка, що звітує щодня "усе гаразд", перетворюється на шум і її
перестають читати (рівно те, що сталось із прапорцем "нестабільні
ревізії", коли він висів на 8 з 11 активів).

**Чому НЕ переписує дані.** Критичне правило 6 — сирі дані
append-only, нічого не видаляємо й не правимо. Звірка лише КАЖЕ, що
джерела розійшлись; що з цим робити — рішення людини, бо причини
бувають різні (різновид ряду, ревізія на одному боці, справжня
помилка).

Використання:
    python crosscheck_macro.py
    python crosscheck_macro.py --notify    # + Telegram при розбіжності
"""

import argparse
import logging
import os
import sys
from datetime import date

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data-ingestion"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reporting"))

from common.consensus import SourceValue, reconcile  # noqa: E402
from common.db import get_connection, insert_observations  # noqa: E402
from config import MACRO_CONSENSUS_TOLERANCE_PCT  # noqa: E402
from macro.bls_adapter import fetch_all as fetch_bls_all  # noqa: E402
from metric_sources import CROSSCHECKABLE_METRICS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def fetch_fred_value(conn, metric_id: str, observed_at: date):
    """Значення FRED за КОНКРЕТНУ дату спостереження.

    Саме за дату, не "останнє": порівнювати останнє значення FRED з
    останнім значенням BLS — помилка, бо джерела публікують із різним
    лагом, і різниця дат читалась би як різниця ЗНАЧЕНЬ. Звіряти можна
    лише один і той самий період."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT value FROM raw_observations
            WHERE source = 'fred' AND metric_id = %s AND observed_at = %s
            ORDER BY revision DESC
            LIMIT 1
            """,
            (metric_id, observed_at),
        )
        row = cur.fetchone()
    return row[0] if row else None


def crosscheck(conn, bls_data: dict) -> list[dict]:
    """[{metric_id, observed_at, fred, bls, verdict}] по кожному
    показнику, який удалось звірити."""
    results = []
    tolerance = MACRO_CONSENSUS_TOLERANCE_PCT

    for metric_id in CROSSCHECKABLE_METRICS:
        records = bls_data.get(metric_id)
        if not records:
            logger.warning("%s: BLS не віддав даних — звірити нічим", metric_id)
            continue

        newest = max(records, key=lambda r: r.observed_at)
        fred_value = fetch_fred_value(conn, metric_id, newest.observed_at)
        if fred_value is None:
            # Нормально: BLS опублікував період, який FRED ще не
            # передрукував (або ми його ще не зібрали). Це не
            # розбіжність — це різний лаг.
            logger.info(
                "%s: BLS має %s, у FRED цього періоду ще немає — звірку відкладено",
                metric_id, newest.observed_at,
            )
            continue

        verdict = reconcile(
            metric_id,
            [SourceValue("fred", fred_value), SourceValue("bls", newest.value)],
            tolerance,
        )
        results.append({
            "metric_id": metric_id,
            "observed_at": newest.observed_at,
            "fred": fred_value,
            "bls": newest.value,
            "verdict": verdict,
        })
    return results


def format_alert(mismatches: list[dict]) -> str:
    lines = ["⚠️ <b>Макродані: джерела розійшлись</b>", ""]
    for item in mismatches:
        verdict = item["verdict"]
        spread = verdict.max_spread_pct
        lines.append(
            f"• <b>{item['metric_id']}</b> за {item['observed_at']}\n"
            f"  FRED: {item['fred']} | BLS: {item['bls']}"
            + (f" | розкид {spread:.3f}%" if spread is not None else "")
        )
    lines.append("")
    lines.append(
        "BLS — першоджерело, FRED — дзеркало. Розбіжність означає або "
        "різновид ряду (згладжений/незгладжений), або ревізію на одному "
        "боці, або помилку. Сирі дані не змінено (правило 6)."
    )
    return "\n".join(lines)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--notify", action="store_true",
        help="надіслати Telegram-алерт, ЯКЩО є розбіжність (при згоді — тиша)",
    )
    parser.add_argument(
        "--store", action="store_true",
        help="зберегти значення BLS у raw_observations як друге джерело",
    )
    args = parser.parse_args()

    bls_data = fetch_bls_all(api_key=os.environ.get("BLS_API_KEY"))
    logger.info("BLS: отримано %d показників", len(bls_data))

    conn = get_connection()
    try:
        if args.store:
            total = 0
            for metric_id, records in bls_data.items():
                total += insert_observations(conn, records)
            logger.info("Збережено значень BLS: %d", total)

        results = crosscheck(conn, bls_data)
        mismatches = [r for r in results if r["verdict"].status != "agree"]

        for item in results:
            verdict = item["verdict"]
            spread = verdict.max_spread_pct
            logger.info(
                "%s за %s: FRED=%s BLS=%s — %s%s",
                item["metric_id"], item["observed_at"], item["fred"], item["bls"],
                verdict.status,
                f" (розкид {spread:.4f}%)" if spread is not None else "",
            )

        if not results:
            logger.info("Нічого не звірено — немає спільних періодів")
            return

        if not mismatches:
            logger.info(
                "Звірено %d показників, розбіжностей немає — сповіщення НЕ "
                "надсилається (звірка, що звітує щодня 'усе гаразд', стає шумом)",
                len(results),
            )
            return

        logger.warning("РОЗБІЖНОСТІ: %d з %d", len(mismatches), len(results))
        if args.notify:
            from telegram_client import send_telegram_message  # noqa: E402
            from _common import resolve_telegram_credentials  # noqa: E402

            token, chat_id = resolve_telegram_credentials()
            send_telegram_message(token, chat_id, format_alert(mismatches), parse_mode="HTML")
            logger.info("Надіслано в Telegram: %d розбіжностей", len(mismatches))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
