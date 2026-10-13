#!/usr/bin/env python3
"""
Ранкове повідомлення в Telegram про запланований календар релізів
(docs/decisions.md 2026-10-03, рішення користувача): у понеділок —
огляд на весь тиждень, у кожен інший робочий день — на сьогодні, з
коротким висновком LLM про вплив на ринок (analysis/calendar_outlook/).
Тільки форматування вже готового результату, жодної аналітики
(reporting/CLAUDE.md).

Використання (після analysis/calendar_outlook/run_outlook.py):
    python calendar_notify.py
"""

import logging

from dotenv import load_dotenv

# _common додає data-ingestion у sys.path (дефіс у назві теки —
# не валідне ім'я Python-пакета), тому імпортується ПЕРШИМ.
from _common import (  # noqa: E402
    DIRECTION_EMOJI,
    bold,
    escape_html,
    fetch_dicts,
    format_local_dt,
    mark_notified,
    resolve_telegram_credentials,
)
from common.db import get_connection  # noqa: E402
from telegram_client import send_telegram_message  # noqa: E402
from telegram_notify import METRIC_LABELS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

IMPACT_EMOJI = {"high": "🔴", "medium": "🟡", "low": "⚪"}

SCOPE_TITLE = {"week": "📅 Огляд тижня", "day": "📅 Огляд дня"}


def fetch_unnotified_outlooks(conn) -> list[dict]:
    return fetch_dicts(
        conn,
        """
        SELECT id, outlook_date, scope, release_log_ids, direction, confidence, summary
        FROM calendar_outlook
        WHERE notified_at IS NULL
        ORDER BY outlook_date
        """,
    )


def fetch_release_rows(conn, release_log_ids: list[int]) -> list[dict]:
    if not release_log_ids:
        return []
    return fetch_dicts(
        conn,
        """
        SELECT source, metric_id, scheduled_at, impact_level
        FROM release_log
        WHERE id = ANY(%s)
        ORDER BY scheduled_at
        """,
        (release_log_ids,),
    )


def format_outlook_message(row: dict, release_rows: list[dict]) -> str:
    title = SCOPE_TITLE.get(row["scope"], "📅 Огляд календаря")
    lines = [bold(f"{title} — {row['outlook_date']}")]

    if not release_rows:
        lines.append("Запланованих релізів немає.")
    else:
        for r in release_rows:
            label = METRIC_LABELS.get(r["metric_id"], r["metric_id"])
            emoji = IMPACT_EMOJI.get(r["impact_level"], "❔")
            lines.append(
                f"{emoji} {escape_html(format_local_dt(r['scheduled_at']))} — "
                f"{escape_html(label)}"
            )

    lines.append("")
    direction_emoji = DIRECTION_EMOJI.get(row["direction"], "❓")
    lines.append(f"{direction_emoji} {escape_html(row['summary'])}")
    return "\n".join(lines)


def main() -> None:
    load_dotenv()
    token, chat_id = resolve_telegram_credentials()

    conn = get_connection()
    try:
        rows = fetch_unnotified_outlooks(conn)
        if not rows:
            logger.info("Нових оглядів календаря немає — сповіщення не надсилається")
            return

        for row in rows:
            release_rows = fetch_release_rows(conn, row["release_log_ids"])
            text = format_outlook_message(row, release_rows)
            send_telegram_message(token, chat_id, text, parse_mode="HTML")
            mark_notified(conn, "calendar_outlook", [row["id"]])
        logger.info("Надіслано в Telegram: %d огляд(и) календаря", len(rows))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
