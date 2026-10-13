#!/usr/bin/env python3
"""
Telegram-звіт: прогноз РІШЕННЯ по ставці (`policy_rate_synthesis`).

Останній незакритий пункт плану закривається саме тут: синтез міг би
лежати в БД і нікуди не доходити — рівно та помилка, що вже траплялась
із скринінгом ("результат рахувався щодня, але ніколи не доходив до
користувача", docs/decisions.md 2026-09-28).

Дедуп — `notified_at`. Повторний синтез того самого засідання з
новішими входами (вийшли дані, відбувся виступ) скидає `notified_at`
у NULL, тож ОНОВЛЕНИЙ прогноз доходить ще раз — і це правильно: зміна
вердикту перед засіданням це і є новина.
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import (  # noqa: E402
    bold,
    escape_html,
    fetch_dicts,
    format_local_dt,
    mark_notified,
    resolve_telegram_credentials,
)
from common.db import get_connection  # noqa: E402
from telegram_client import send_telegram_message  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BANK_LABELS = {
    "fed": "🇺🇸 Федеральна резервна система",
    "ecb": "🇪🇺 Європейський центральний банк",
    "boj": "🇯🇵 Банк Японії",
}

DECISION_LABELS = {
    "hold": "⏸ УТРИМАЄ ставку",
    "cut": "🔻 ЗНИЗИТЬ ставку",
    "hike": "🔺 ПІДВИЩИТЬ ставку",
    "unclear": "❓ напрямок неясний",
}


def fetch_unnotified(conn, limit: int = 5) -> list[dict]:
    return fetch_dicts(
        conn,
        """
        SELECT id, bank, meeting_at, decision, expected_rate, confidence,
               summary, reasoning, watch_on_release
        FROM policy_rate_synthesis
        WHERE notified_at IS NULL
        ORDER BY meeting_at
        LIMIT %s
        """,
        (limit,),
    )


def format_message(row: dict) -> str:
    bank = BANK_LABELS.get(row["bank"], row["bank"])
    decision = DECISION_LABELS.get(row["decision"], row["decision"])

    lines = [
        bold(f"🏛 Прогноз рішення по ставці — {bank}"),
        "",
        f"Засідання: {format_local_dt(row['meeting_at'])}",
        f"Наш прогноз: {bold(decision)}",
    ]
    if row["expected_rate"] is not None:
        lines.append(f"Очікувана ставка після засідання: {row['expected_rate']}")
    if row["confidence"] is not None:
        lines.append(f"Упевненість: {row['confidence']}")
    lines.append("")
    lines.append(escape_html(row["summary"]))

    if row.get("reasoning"):
        lines.append("")
        lines.append(bold("На чому ґрунтується:"))
        lines.append(escape_html(row["reasoning"]))

    if row.get("watch_on_release"):
        lines.append("")
        lines.append(bold("🔎 На що звернути увагу:"))
        lines.append(escape_html(row["watch_on_release"]))

    lines.append("")
    lines.append(
        "ℹ️ Прогноз РІШЕННЯ засідання, не вказівка діяти. Зведено з п'яти "
        "входів: макродані, наші прогнози по них, крива дохідностей, "
        "новини, виступи членів комітету."
    )
    return "\n".join(lines)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    token, chat_id = resolve_telegram_credentials()

    conn = get_connection()
    try:
        rows = fetch_unnotified(conn, limit=args.limit)
        if not rows:
            logger.info("Нових прогнозів по ставці немає — сповіщення не надсилається")
            return

        # ОДНЕ повідомлення на банк, не батч: розбір рішення довгий
        # (summary + обґрунтування + на що дивитись), і три банки в
        # одному впираються в ліміт Telegram 4096 символів — той самий
        # живий фікс, що вже застосовано в release_impact_notify.py.
        for row in rows:
            send_telegram_message(token, chat_id, format_message(row), parse_mode="HTML")
            mark_notified(conn, "policy_rate_synthesis", [row["id"]])
            logger.info(
                "Надіслано в Telegram: %s, засідання %s, рішення %s",
                row["bank"], row["meeting_at"].date(), row["decision"],
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
