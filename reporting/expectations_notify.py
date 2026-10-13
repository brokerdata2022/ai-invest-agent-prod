#!/usr/bin/env python3
"""
Сповіщення в Telegram про сюрприз факт/очікування — уже пораховане
analysis/expectations/compare_releases.py (expectation_comparisons) і,
якщо встиг відпрацювати synthesize.py, доповнене причинним LLM-
висновком (expectation_synthesis) — "вийшло X, очікувалось Y, це
означає Z" (docs/decisions.md, критерій завершення Фази 2). Тільки
форматування готового результату, жодної аналітики (reporting/CLAUDE.md).
Синтез — LEFT JOIN: якщо ще не встиг (той самий цикл) або LLM-виклик
впав, повідомлення все одно йде із самими цифрами — не блокується на
LLM-кроці.

СВІДОМО короткий — лише факт/очікування/сюрприз (+ 1-2 речення
причинного висновку). Комплексний розбір впливу на інші категорії
активів (ставка/економіка/валюта/акції/крипта/золото/інший актив),
який той самий synthesize.py вже порахував (expectation_synthesis.
asset_impacts) — ОКРЕМЕ повідомлення, `reporting/release_impact_notify.py`
(2026-10-02, рішення користувача: "сюрприз лишається сюрпризом, а
широкий аналіз ринку — це новий [звіт]", щоб короткий сюрприз-звіт не
розбухав і далі приходив вчасно).

За замовчуванням — тільки impact_level high/medium (release_log):
low-показники не варті окремого сповіщення (той самий принцип, що
"короткий звіт, не повний дамп", reporting/CLAUDE.md).

Надсилає ЛИШЕ ще не надіслані порівняння (notified_at IS NULL) і
взагалі НІЧОГО не шле в Telegram, якщо таких немає — на відміну від
news_notify.py/synthesis_notify.py (свідомо "завжди останній стан",
бо там майже завжди є свіжий контент кожного циклу), сюрпризи
факт/очікування — рідкісна подія (кілька релізів на місяць), а
notify_expectations стоїть у розкладі щохвилини (2026-10-02): без
цього фільтра скрипт спамив би тим самим "Нових порівнянь немає" щоцикл цілодобово
(живий баг, знайдений під час розгортання з нуля — docs/decisions.md,
2026-09-27). Побічний ефект фільтра — природне групування: якщо
кілька показників вийшли й пораховані в тому самому циклі
compare_expectations, вони підуть ОДНИМ повідомленням (rows тут може
містити кілька рядків), а не окремими.

Використання (після analysis/expectations/compare_releases.py):
    python expectations_notify.py --limit 5
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

# _common додає data-ingestion у sys.path (дефіс у назві теки —
# не валідне ім'я Python-пакета), тому імпортується ПЕРШИМ.
from _common import bold, escape_html, fetch_dicts, resolve_telegram_credentials  # noqa: E402
from common.db import get_connection  # noqa: E402
from telegram_client import send_telegram_message  # noqa: E402
from telegram_notify import METRIC_LABELS  # noqa: E402

# Єдиний reporting-скрипт, що ПИШЕ в БД (mark_notified), тому єдиний,
# кому потрібен ще й analysis/ у sys.path — тримаємо вставку тут, а не
# в _common.py, щоб решта reporting/ лишалась незалежною від analysis/
# (reporting/CLAUDE.md).
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analysis")
)
from expectations._db import mark_notified  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_IMPACT_LEVELS = ("high", "medium")


def fetch_recent_comparisons(
    conn, limit: int = 5, impact_levels: tuple = DEFAULT_IMPACT_LEVELS
) -> list[dict]:
    """Лише ще НЕ надіслані (notified_at IS NULL) — виклик main() відповідає
    за mark_notified() після успішної відправки."""
    query = """
        SELECT ec.id, ec.metric_id, ec.source, ec.observed_at, ec.actual_value,
               ec.expected_value_raw, ec.expected_value_parsed, ec.surprise,
               ec.surprise_pct, ec.comparison_method, ec.created_at,
               rl.impact_level, es.summary AS synthesis_summary
        FROM expectation_comparisons ec
        JOIN release_log rl ON rl.id = ec.release_log_id
        LEFT JOIN expectation_synthesis es ON es.comparison_id = ec.id
        WHERE rl.impact_level = ANY(%s)
          AND ec.notified_at IS NULL
        ORDER BY ec.created_at DESC
        LIMIT %s
    """
    return fetch_dicts(conn, query, (list(impact_levels), limit))


def format_message(rows: list[dict]) -> str:
    if not rows:
        return "📊 Нових порівнянь факт/очікування немає."

    lines = [bold(f"📊 Факт vs очікування ({len(rows)}):"), ""]
    for row in rows:
        label = METRIC_LABELS.get(row["metric_id"], row["metric_id"])
        pct_suffix = f" ({row['surprise_pct']:+.1f}%)" if row["surprise_pct"] is not None else ""
        lines.append(bold(f"{label} — {row['observed_at']}"))
        lines.append(
            f"Факт {float(row['actual_value']):.4g} vs очікування "
            f"{float(row['expected_value_parsed']):.4g} (прогноз: {escape_html(row['expected_value_raw'])})"
        )
        lines.append(f"Сюрприз: {float(row['surprise']):+.4g}{pct_suffix}")
        if row.get("synthesis_summary"):
            lines.append(f"→ {escape_html(row['synthesis_summary'])}")
        lines.append("")
    return "\n".join(lines).rstrip()


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--impact", nargs="+", default=list(DEFAULT_IMPACT_LEVELS),
        help="рівні impact_level для показу (за замовчуванням high medium)",
    )
    args = parser.parse_args()

    token, chat_id = resolve_telegram_credentials()

    conn = get_connection()
    try:
        rows = fetch_recent_comparisons(conn, limit=args.limit, impact_levels=tuple(args.impact))
        if not rows:
            logger.info("Нових порівнянь немає — сповіщення не надсилається")
            return

        text = format_message(rows)
        send_telegram_message(token, chat_id, text, parse_mode="HTML")
        mark_notified(conn, [row["id"] for row in rows])
        logger.info("Надіслано в Telegram: %d порівнянь", len(rows))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
