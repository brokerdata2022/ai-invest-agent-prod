#!/usr/bin/env python3
"""
Сповіщення в Telegram про останній прогін LONG-скринінгу крипто-
ф'ючерсів — вже пораховано analysis/crypto_screening/run_screening.py
(crypto_long_candidates: символи з підтвердженим тренд+OI+RSI+funding,
long_screen.py:screen_long()). Тільки форматування готового
результату, жодної аналітики (reporting/CLAUDE.md).

Раніше LONG-кандидати лише логувались (run_screening.py:main()),
ніколи не персистувались у БД і ніколи не йшли в Telegram — рішення
користувача 2026-10-02: закрити цей пропуск поруч із SHORT/WATCH
(crypto_screening_notify.py) перед живим тестуванням усіх трьох
notify-скриптів разом.

Тон (analysis/CLAUDE.md "Заборонені формулювання", той самий принцип
стосується reporting/): "бичачий нахил", НЕ "купуй"/"входь у лонг".

Дедуп (той самий принцип, що screening_notify.py — акції): рядки
ОСТАННЬОГО run_at (run_screening.py вставляє один run_at на весь
денний прогін), WHERE notified_at IS NULL.

Використання (після analysis/crypto_screening/run_screening.py):
    python crypto_long_notify.py
    python crypto_long_notify.py --limit 15
"""

import argparse
import logging

from dotenv import load_dotenv

# _common додає data-ingestion у sys.path (дефіс у назві теки —
# не валідне ім'я Python-пакета), тому імпортується ПЕРШИМ.
from _common import (  # noqa: E402
    bold, composition_changed, fetch_dicts, mark_notified,
    remember_composition, resolve_telegram_credentials,
)
from common.db import get_connection  # noqa: E402
from telegram_client import send_telegram_message  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Ключ для _common.composition_changed — дорівнює назві джоби.
NOTIFIER = "notify_crypto_long"


def fetch_unnotified_long(conn) -> list[dict]:
    """УСІ рядки останнього прогону (MAX(run_at)), ще не надіслані —
    не лише --limit, щоб mark_notified() закрив ВЕСЬ run_at одразу
    (той самий принцип, що screening_notify.py:fetch_unnotified_screening)."""
    return fetch_dicts(
        conn,
        """
        SELECT id, symbol, oi_change_pct, rsi_value, funding_rate, run_at
        FROM crypto_long_candidates
        WHERE run_at = (SELECT MAX(run_at) FROM crypto_long_candidates)
          AND notified_at IS NULL
        ORDER BY symbol
        """,
    )


def format_message(rows: list[dict], limit: int = 30) -> str:
    if not rows:
        return "📈 Нових LONG-кандидатів немає."

    total = len(rows)
    shown = rows[:limit]
    header = f"📈 Крипто-скринінг LONG (бичачий нахил, тренд підтверджений) — {total} символів"
    if total > limit:
        header += f" (топ {limit})"
    lines = [bold(header), ""]
    for row in shown:
        oi = row.get("oi_change_pct")
        rsi = row.get("rsi_value")
        oi_str = f"{float(oi):+.1f}%" if oi is not None else "н/д"
        rsi_str = f"{float(rsi):.0f}" if rsi is not None else "н/д"
        lines.append(f"• {bold(row['symbol'])} — OI {oi_str}, RSI {rsi_str}")
    return "\n".join(lines).rstrip()


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=30, help="Скільки символів показати (за замовчуванням 30)"
    )
    args = parser.parse_args()

    token, chat_id = resolve_telegram_credentials()

    conn = get_connection()
    try:
        rows = fetch_unnotified_long(conn)
        if not rows:
            logger.info("Нового LONG-скринінгу немає — сповіщення не надсилається")
            return

        # Живий привід (12 жовтня): той самий 1-2 кандидати
        # надсилались 4 рази за добу (джоба о 0/6/12/18).
        symbols = [row["symbol"] for row in rows]
        if not composition_changed(conn, NOTIFIER, symbols):
            mark_notified(conn, "crypto_long_candidates", [row["id"] for row in rows])
            logger.info("Склад LONG не змінився (%d) — не надсилаємо", len(symbols))
            return

        text = format_message(rows, limit=args.limit)
        send_telegram_message(token, chat_id, text, parse_mode="HTML")
        mark_notified(conn, "crypto_long_candidates", [row["id"] for row in rows])
        remember_composition(conn, NOTIFIER, symbols)
        logger.info("Надіслано в Telegram: LONG-скринінг, %d символів", len(rows))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
