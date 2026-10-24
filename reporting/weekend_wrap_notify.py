#!/usr/bin/env python3
"""
Надсилає в Telegram ПІДСУМОК ВИХІДНИХ (`weekend_wrap` —
`analysis/news_analysis/weekend_wrap.py`, 2026-10-24): що за вихідні
може дати геп на відкритті в понеділок. Тільки форматування готового
результату (reporting/CLAUDE.md); дедуп — `notified_at IS NULL`.

Використання (після analysis/news_analysis/weekend_wrap.py):
    python weekend_wrap_notify.py
    python weekend_wrap_notify.py --dry-run
"""

import argparse
import logging

from dotenv import load_dotenv

# _common додає data-ingestion у sys.path, тому імпортується ПЕРШИМ.
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


def fetch_unnotified(conn):
    return fetch_one_dict(
        conn,
        """
        SELECT id, wrap_date, summary, direction, confidence, gap_risks, crypto, watch_monday
        FROM weekend_wrap
        WHERE notified_at IS NULL
        ORDER BY wrap_date DESC
        LIMIT 1
        """,
    )


def format_message(row: dict) -> str:
    direction = MARKET_DIRECTION_LABEL.get(row["direction"], row["direction"])
    lines = [
        bold(f"🗓 Підсумок вихідних · {row['wrap_date']}"),
        f"{escape_html(direction)} на відкриття · впевненість {float(row['confidence']):.2f}",
        "",
        escape_html(row["summary"]),
    ]
    gap_risks = row.get("gap_risks") or []
    if gap_risks:
        lines.append("")
        lines.append(bold("Можливий геп:"))
        lines.extend(f"⚡ {escape_html(item)}" for item in gap_risks)
    if row.get("crypto"):
        lines.append("")
        lines.append(f"{bold('Крипта:')} {escape_html(row['crypto'])}")
    watch = row.get("watch_monday") or []
    if watch:
        lines.append("")
        lines.append(bold("У понеділок першим:"))
        lines.extend(f"👁 {escape_html(item)}" for item in watch)
    lines.append("")
    lines.append("<i>ℹ️ Опис ризиків відкриття, не вказівка діяти.</i>")
    return "\n".join(lines)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="надрукувати, не надсилати")
    args = parser.parse_args()

    conn = get_connection()
    try:
        row = fetch_unnotified(conn)
        if row is None:
            logger.info("Нового підсумку вихідних немає — не надсилаємо")
            return
        text = format_message(row)
        if args.dry_run:
            print(text)
            return
        token, chat_id = resolve_telegram_credentials()
        send_telegram_message(token, chat_id, text, parse_mode="HTML")
        mark_notified(conn, "weekend_wrap", [row["id"]])
        logger.info("Надіслано в Telegram: підсумок вихідних %s", row["wrap_date"])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
