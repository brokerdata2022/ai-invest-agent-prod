#!/usr/bin/env python3
"""
Сповіщення в Telegram про КОМПЛЕКСНИЙ вплив макрорелізу на інші
категорії активів — ставка/економіка/валюта/акції/крипта/золото/інший
актив (`expectation_synthesis.asset_impacts`, уже пораховане
`analysis/expectations/synthesize.py`). Тільки форматування готового
результату, жодної аналітики (reporting/CLAUDE.md).

ОКРЕМЕ повідомлення від `expectations_notify.py` (2026-10-02, рішення
користувача: "сюрприз лишається сюрпризом, а широкий аналіз ринку — це
новий [звіт]") — короткий сюрприз-звіт не повинен розбухати й
затримуватись через довший широкий розбір; дедуп теж окремий
(`expectation_synthesis.impact_notified_at`, НЕ
`expectation_comparisons.notified_at`, яким керує expectations_notify.py)
— обидва звіти незалежно відстежують, що вже надіслали.

ОДНЕ повідомлення НА ОДИН реліз (не батч кількох разом) — той самий
живий фікс, що вже застосовано в synthesis_notify.py 2026-10-02:
кілька релізів з повним розбором impacts у ОДНОМУ повідомленні
реально впираються в ліміт Telegram (4096 символів).

Шле ЛИШЕ рядки, де LLM реально знайшов зачеплені категорії
(`asset_impacts` непорожній) — рядок без жодного міжактивного впливу
(рідкісний випадок: сюрприз занадто малий, щоб на щось вплинути) просто
не генерує це сповіщення, не "Вплив: немає".

Використання (після analysis/expectations/synthesize.py):
    python release_impact_notify.py --limit 5
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

# _common додає data-ingestion у sys.path (дефіс у назві теки —
# не валідне ім'я Python-пакета), тому імпортується ПЕРШИМ.
from _common import DIRECTION_EMOJI, bold, escape_html, fetch_dicts, resolve_telegram_credentials  # noqa: E402
from common.db import get_connection  # noqa: E402
from telegram_client import send_telegram_message  # noqa: E402
from telegram_notify import METRIC_LABELS  # noqa: E402

# Другий (після expectations_notify.py) свідомий виняток із "reporting/
# не імпортує з analysis/" (reporting/CLAUDE.md) — пише
# impact_notified_at через expectations._db.mark_impact_notified.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analysis")
)
from expectations._db import mark_impact_notified  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def fetch_unnotified_impacts(conn, limit: int = 5) -> list[dict]:
    """Лише синтези з непорожнім asset_impacts і ще НЕ надісланим цим
    звітом (impact_notified_at IS NULL) — окремий дедуп від
    expectations_notify.py (докстрінг модуля)."""
    query = """
        SELECT es.id AS synthesis_id, es.asset_impacts, es.summary,
               ec.metric_id, ec.observed_at
        FROM expectation_synthesis es
        JOIN expectation_comparisons ec ON ec.id = es.comparison_id
        WHERE es.asset_impacts IS NOT NULL
          AND jsonb_array_length(es.asset_impacts) > 0
          AND es.impact_notified_at IS NULL
        ORDER BY es.created_at DESC
        LIMIT %s
    """
    return fetch_dicts(conn, query, (limit,))


def _format_category(category: str) -> str:
    """'інший_актив' → 'Інший актив' — лише косметика відображення,
    сама категорія лишається такою, як прийшла від LLM
    (analysis/expectations/synthesize.py:IMPACT_CATEGORIES)."""
    return category.replace("_", " ").capitalize()


def format_impact_message(row: dict) -> str:
    label = METRIC_LABELS.get(row["metric_id"], row["metric_id"])
    lines = [
        bold(f"🌐 Вплив релізу на ринок: {label} — {row['observed_at']}"),
        escape_html(row["summary"]),
        "",
    ]
    for impact in row["asset_impacts"]:
        emoji = DIRECTION_EMOJI.get(impact.get("direction"), "❓")
        category = _format_category(impact.get("category", ""))
        assets = impact.get("assets")
        header = f"{emoji} {bold(category)}" + (f" ({escape_html(assets)})" if assets else "")
        lines.append(header)
        if impact.get("explanation"):
            lines.append(f"  {escape_html(impact['explanation'])}")
    return "\n".join(lines).rstrip()


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    token, chat_id = resolve_telegram_credentials()

    conn = get_connection()
    try:
        rows = fetch_unnotified_impacts(conn, limit=args.limit)
        if not rows:
            logger.info("Нових розборів впливу немає — сповіщення не надсилається")
            return

        # Одне повідомлення на один реліз, notified одразу після
        # кожного успішного надсилання — той самий принцип, що
        # synthesis_notify.py (мережевий збій на половині списку не
        # повинен дублювати вже надіслане на ретраї).
        for row in rows:
            text = format_impact_message(row)
            send_telegram_message(token, chat_id, text, parse_mode="HTML")
            mark_impact_notified(conn, [row["synthesis_id"]])
        logger.info("Надіслано в Telegram: %d розборів впливу", len(rows))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
