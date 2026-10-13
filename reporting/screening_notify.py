#!/usr/bin/env python3
"""
Сповіщення в Telegram про останній прогін скринінгу акцій S&P 500 —
вже порахований analysis/screening/composite_score.py (screening_results,
Tier A→B→C + ранжування). Тільки форматування готового результату,
жодної аналітики (reporting/CLAUDE.md).

Раніше для screening_results не існувало жодного notify-скрипта — список
активів рахувався щодня (screening_composite_score@05:00), але ніколи не
потрапляв у Telegram (docs/decisions.md, розділ 3а №1).

Повідомлення показує тикер, назву компанії, зміну ціни за 24 години й
зміну обсягу торгів за 24 години у ВІДСОТКАХ (живий фідбек користувача,
2026-10-02: score/revenue/eps/pe/avg_dollar_volume без контексту
"страшні"; дата — коли спитали "на яку дату ці дані?"; обсяг спершу
зробили в доларах, тим самим днем уточнили — "в відсотках зміни") —
усі поля рахує й зберігає composite_score.py:enrich_with_report_context(),
score і далі визначає лише ПОРЯДОК (сортування), сам не показується.
Дата в заголовку — дата останньої ціни (price_date), не run_at самого
скринінгу (можуть відрізнятись на кілька годин).

Дедуп (той самий принцип, що в усіх notify-скриптах проєкту):
`notified_at IS NULL` на рядках ОСТАННЬОГО run_at — composite_score.py
вставляє один run_at на весь щоденний прогін, тож повторний запуск цього
скрипта (напр. ретрай runner.py) не надішле той самий список удруге.

Застарілі ціни окремих тикерів (2026-10-04, critical rule 7 CLAUDE.md —
той самий принцип, що reporting/watchlist_notify.py): заголовок показує
НАЙСВІЖІШУ дату серед рядків, тому один тикер, якому Twelve Data
відмовила через денний ліміт (~800 запитів/добу, найвужче місце
конвеєра — data-ingestion/CLAUDE.md) і чия ціна тому застрягла на
кілька днів, НЕ впадав би в очі на фоні свіжого заголовка — та сама
прогалина, що вже виправлена для watchlist-активів, тепер і тут через
спільний `common/freshness.py:is_stale()`.

Використання (після analysis/screening/composite_score.py):
    python screening_notify.py
    python screening_notify.py --limit 10
"""

import argparse
import logging

from dotenv import load_dotenv

# _common додає data-ingestion у sys.path (дефіс у назві теки —
# не валідне ім'я Python-пакета), тому імпортується ПЕРШИМ.
from _common import (  # noqa: E402
    bold, composition_changed, escape_html, fetch_dicts, mark_notified,
    remember_composition, resolve_telegram_credentials,
)
from common.db import get_connection  # noqa: E402
from common.freshness import is_stale  # noqa: E402
from telegram_client import send_telegram_message  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Ключ для _common.composition_changed — стабільний, дорівнює назві джоби.
NOTIFIER = "notify_screening"


def fetch_unnotified_screening(conn) -> list[dict]:
    """УСІ рядки останнього прогону (MAX(run_at)), ще не надіслані —
    не лише --limit найкращих, щоб mark_notified() нижче закрив ВЕСЬ
    run_at одразу, а не лишав "хвіст" на наступний виклик. score —
    лише для сортування (ранжування Tier A→B→C), у повідомленні не
    показується (живий фідбек користувача, 2026-10-02)."""
    return fetch_dicts(
        conn,
        """
        SELECT id, ticker, company_name, price_change_24h_pct, price_date,
               volume_change_24h_pct, score, run_at
        FROM screening_results
        WHERE run_at = (SELECT MAX(run_at) FROM screening_results)
          AND notified_at IS NULL
        ORDER BY score DESC
        """,
    )


def format_message(rows: list[dict], limit: int = 20) -> str:
    if not rows:
        return "📊 Нового скринінгу S&P 500 немає."

    total = len(rows)
    shown = rows[:limit]

    # Дата в заголовку — дата ОСТАННЬОЇ ціни (price_date), не run_at
    # самого скринінгу (живий фідбек користувача, 2026-10-02: "на яку
    # дату ці дані?") — найпізніша серед рядків, якщо раптом розійшлись.
    price_dates = [row["price_date"] for row in rows if row.get("price_date")]
    date_str = f" (дані на {max(price_dates).isoformat()})" if price_dates else ""

    header = f"📊 Скринінг S&P 500 — пройшли Tier A→B→C, {total} тикерів{date_str}"
    if total > limit:
        header += f" (топ {limit})"
    lines = [bold(header), ""]
    for i, row in enumerate(shown, start=1):
        change = row.get("price_change_24h_pct")
        change_str = f"{float(change):+.2f}%" if change is not None else "н/д"
        volume_change = row.get("volume_change_24h_pct")
        volume_str = f"{float(volume_change):+.1f}%" if volume_change is not None else "н/д"
        name = row.get("company_name") or row["ticker"]
        lines.append(f"{i}. {bold(row['ticker'])} — {escape_html(name)} | 24г: {change_str} | обсяг: {volume_str}")

    # ⚠️ (2026-10-04, common/freshness.py:is_stale()) — ЦЕ активна
    # перевірка (не лише заголовок з найсвіжішою датою): тикер, чия
    # ціна відстала через збій/ліміт Twelve Data, названий явно, навіть
    # якщо він не потрапив у --limit найкращих.
    stale_tickers = [row["ticker"] for row in rows if row.get("price_date") and is_stale(row["price_date"])]
    if stale_tickers:
        lines.append("")
        lines.append(f"⚠️ Застарілі ціни: {escape_html(', '.join(stale_tickers))}")

    return "\n".join(lines).rstrip()


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=20, help="Скільки тикерів показати (за замовчуванням 20)"
    )
    args = parser.parse_args()

    token, chat_id = resolve_telegram_credentials()

    conn = get_connection()
    try:
        rows = fetch_unnotified_screening(conn)
        if not rows:
            logger.info("Нового скринінгу немає — сповіщення не надсилається")
            return

        # Склад не змінився — не шлемо (2026-10-13). Живий привід:
        # цей скрипт надіслав ІДЕНТИЧНІ 14 тикерів 13-й день підряд.
        # Рядки щоразу нові (новий run_at), тому mark_notified їх не
        # зупиняв — потрібне порівняння СКЛАДУ (_common.py:
        # composition_changed).
        tickers = [row["ticker"] for row in rows]
        if not composition_changed(conn, NOTIFIER, tickers):
            mark_notified(conn, "screening_results", [row["id"] for row in rows])
            logger.info(
                "Склад скринінгу не змінився (%d тикерів) — не надсилаємо, "
                "рядки позначено надісланими", len(tickers),
            )
            return

        text = format_message(rows, limit=args.limit)
        send_telegram_message(token, chat_id, text, parse_mode="HTML")
        mark_notified(conn, "screening_results", [row["id"] for row in rows])
        remember_composition(conn, NOTIFIER, tickers)
        logger.info("Надіслано в Telegram: скринінг S&P 500, %d тикерів", len(rows))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
