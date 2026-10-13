#!/usr/bin/env python3
"""
Сповіщення в Telegram про причинну атрибуцію ціна↔новини — вже
пораховану analysis/news_analysis/synthesize.py (news_synthesis).
Тільки форматування готового результату, жодної аналітики
(reporting/CLAUDE.md).

Дедуп (2026-09-28, критичний фікс, той самий принцип що
news_notify.py): `notified_at IS NULL` — без цього повторний запуск
джоби (напр. ретрай runner.py при транзієнтному збої відправки) слав
би ті самі рядки вдруге.

Використання (після analysis/news_analysis/synthesize.py):
    python synthesis_notify.py --limit 5
"""

import argparse
import logging

from dotenv import load_dotenv

# _common додає data-ingestion у sys.path (дефіс у назві теки —
# не валідне ім'я Python-пакета), тому імпортується ПЕРШИМ.
from _common import (  # noqa: E402
    DIRECTION_EMOJI,
    bold,
    escape_html,
    fetch_dicts,
    mark_notified,
    resolve_telegram_credentials,
)
from common.db import get_connection  # noqa: E402
from telegram_client import send_telegram_message  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def fetch_unnotified_synthesis(conn, limit: int = 5) -> list[dict]:
    query = """
        SELECT id, asset_id, cluster_count, net_lean, price_pct_change,
               price_start_date, price_end_date, direction, confidence,
               summary, confirmation_factors, created_at
        FROM news_synthesis
        WHERE notified_at IS NULL
        ORDER BY created_at DESC
        LIMIT %s
    """
    return fetch_dicts(conn, query, (limit,))


def format_synthesis_message(row: dict) -> str:
    """2026-10-02, живий фідбек користувача: період і
    confirmation_factors (той самий фікс, що reporting/daily_digest.py:
    format_news_synthesis_message()). ОДНЕ повідомлення на ОДИН
    рядок — регресія того самого дня, окремо: разом із новим ширшим
    SYSTEM_PROMPT (analysis/news_analysis/synthesize.py, довші summary/
    confirmation_factors) старий batch-формат (усі рядки в ОДНЕ
    повідомлення) реально впирався в ліміт Telegram 4096 символів на
    кількох рядках одразу (HTTP 400,
    "message is too long") — живо підтверджено користувачем. Той самий
    принцип, що вже є в daily_digest.py: кожна подія окремим
    повідомленням, природно короткий за конструкцією, ліміт ніколи не
    досягається."""
    emoji = DIRECTION_EMOJI.get(row["direction"], "❓")
    header = (
        f"🔍 {emoji} {row['asset_id']}: ціна {float(row['price_pct_change']):+.2f}% "
        f"({row['price_start_date']} → {row['price_end_date']}), "
        f"новини {row['net_lean']:+d} ({row['cluster_count']} історій)"
    )
    lines = [bold(header), escape_html(row["summary"])]
    if row.get("confirmation_factors"):
        lines.append(f"🔎 Перевірити: {escape_html(row['confirmation_factors'])}")
    return "\n".join(lines)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    token, chat_id = resolve_telegram_credentials()

    conn = get_connection()
    try:
        rows = fetch_unnotified_synthesis(conn, limit=args.limit)
        if not rows:
            logger.info("Нових синтезів немає — сповіщення не надсилається")
            return

        # Позначаємо notified_at ОДРАЗУ після кожного успішного
        # надсилання (не одним батчем наприкінці) — той самий принцип,
        # що daily_digest.py: мережевий збій на половині списку не
        # повинен змусити наступний прогін надіслати вже надіслані
        # рядки вдруге.
        for row in rows:
            text = format_synthesis_message(row)
            send_telegram_message(token, chat_id, text, parse_mode="HTML")
            mark_notified(conn, "news_synthesis", [row["id"]])
        logger.info("Надіслано в Telegram: %d синтезів", len(rows))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
