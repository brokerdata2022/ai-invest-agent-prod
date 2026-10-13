#!/usr/bin/env python3
"""
Сповіщення в Telegram про стан ринку (risk-on/risk-off) — вже
пораховане analysis/news_analysis/synthesize_market.py (market_synthesis).
Тільки форматування готового результату, жодної аналітики
(reporting/CLAUDE.md).

Дедуп (2026-09-28, критичний фікс, той самий принцип що
news_notify.py): `notified_at IS NULL` — без цього повторний запуск
джоби (напр. ретрай runner.py при транзієнтному збої відправки) слав
би той самий висновок дня вдруге.

Використання (після analysis/news_analysis/synthesize_market.py):
    python market_notify.py
"""

import argparse
import logging

from dotenv import load_dotenv

# _common додає data-ingestion у sys.path (дефіс у назві теки —
# не валідне ім'я Python-пакета), тому імпортується ПЕРШИМ.
from _common import (  # noqa: E402
    MARKET_DIRECTION_LABEL,
    bold,
    escape_html,
    fetch_one_dict,
    mark_notified,
    resolve_telegram_credentials,
)
from common.db import get_connection  # noqa: E402
from telegram_client import send_telegram_message  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def fetch_unnotified_market_synthesis(conn):
    query = """
        SELECT id, cluster_count, direction, confidence, summary, source_refs,
               created_at, session
        FROM market_synthesis
        WHERE notified_at IS NULL
        ORDER BY created_at DESC
        LIMIT 1
    """
    return fetch_one_dict(conn, query)


# Сесія → заголовок повідомлення. 'daily' — старі рядки до переходу
# на сесійний розклад (db/schema.sql, 2026-10-04).
SESSION_TITLE = {
    "asia": "🌏 Азіатська сесія",
    "europe": "🌍 Європейська сесія",
    "us": "🌎 Американська сесія",
    "daily": "🌍 Стан ринку",
}


def format_message(row) -> str:
    if row is None:
        return "🌍 Синтезу стану ринку ще немає."

    label = MARKET_DIRECTION_LABEL.get(row["direction"], row["direction"])
    confidence = float(row["confidence"])
    # Сесія в заголовку (2026-10-04): з трьома синтезами на добу без неї
    # неможливо зрозуміти, про який момент доби йдеться — а саме момент
    # і визначає, як читати ті самі новини (analysis/news_analysis/
    # synthesize_market.py:SESSIONS).
    session_title = SESSION_TITLE.get(row.get("session") or "daily", "🌍 Стан ринку")
    lines = [
        bold(f"{session_title}: {label} (упевненість {confidence:.2f})"),
        escape_html(row["summary"]),
        "",
        bold(f"На основі {row['cluster_count']} найбільш підтверджених історій:"),
    ]
    for ref in row["source_refs"]:
        lines.append(f"• [{ref['source_count']} джерел] {escape_html(ref['title'])}")
    return "\n".join(lines).rstrip()


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()  # лише для --help/валідації: скрипт без параметрів

    token, chat_id = resolve_telegram_credentials()

    conn = get_connection()
    try:
        row = fetch_unnotified_market_synthesis(conn)
        if row is None:
            logger.info("Нового синтезу стану ринку немає — сповіщення не надсилається")
            return

        text = format_message(row)
        send_telegram_message(token, chat_id, text, parse_mode="HTML")
        mark_notified(conn, "market_synthesis", [row["id"]])
        logger.info("Надіслано в Telegram: стан ринку %s", row["direction"])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
