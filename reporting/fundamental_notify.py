#!/usr/bin/env python3
"""
Надсилає в Telegram фундаментальний LLM-аналіз акцій
(`fundamental_analysis` — `analysis/fundamental/`).

Не дублює `screening_notify.py`: там детермінований результат воронки
Tier A/B/C (тикер/назва/зміна ціни), тут — ІНТЕРПРЕТАЦІЯ: для акцій
звітності, для активів списку обраних — їхніх власних чинників
(золото: реальні ставки й долар; USD/JPY: різниця ставок ФРС/БОЯ).

## Одне повідомлення НА АКТИВ, не одне на всіх

Рішення користувача 2026-10-04: "розгорнутий аналіз по активах із
списку вибраних... користувачу надсилати висновок цього аналізу".
Розгорнутий розбір по чинниках — це ~1200 символів на актив; 11
активів в одному повідомленні впираються в ліміт Telegram 4096
(HTTP 400). Той самий урок, що вже був із `synthesis_notify.py`
(docs/decisions.md, 2026-10-02).

Тільки форматування вже готового результату, жодної аналітики
(reporting/CLAUDE.md). Формулювання описові, без "купити/продати"
(analysis/CLAUDE.md).

Використання (після analysis/fundamental/run_analysis.py):
    python fundamental_notify.py
"""

import argparse
import logging

from dotenv import load_dotenv

# _common додає data-ingestion у sys.path, тому імпортується ПЕРШИМ.
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

DIRECTION_WORD = {
    "up": "картина покращується",
    "down": "картина погіршується",
    "neutral": "картина стабільна",
    "unclear": "картина неоднозначна",
}


def fetch_unnotified(conn, limit: int = 10) -> list[dict]:
    return fetch_dicts(
        conn,
        """
        SELECT id, asset_id, asset_kind, ticker, company_name, direction,
               confidence, summary, reasoning, strengths, risks
        FROM fundamental_analysis
        WHERE notified_at IS NULL
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
    )


ASSET_KIND_TITLE = {
    "stock": "📑 Фундаментальний аналіз",
    "watchlist": "📊 Розбір за чинниками",
}


def format_asset_message(row: dict) -> str:
    """Одне повідомлення про ОДИН актив — розгорнуто.

    `reasoning` тут не технічна деталь, а головний вміст: саме в ньому
    розбір по кожному чиннику окремо, за яким і зрозуміло, ЧОМУ
    висновок такий (рішення користувача 2026-10-04)."""
    name = row.get("company_name") or row["asset_id"]
    title = ASSET_KIND_TITLE.get(row.get("asset_kind") or "stock", "📑 Аналіз")
    emoji = DIRECTION_EMOJI.get(row["direction"], "❓")
    word = DIRECTION_WORD.get(row["direction"], row["direction"])

    lines = [
        bold(f"{title}: {name}"),
        f"{emoji} {escape_html(word)} · впевненість {float(row['confidence']):.2f}",
        "",
        escape_html(row["summary"]),
    ]

    if row.get("reasoning"):
        lines.append("")
        lines.append(bold("Розбір по чинниках:"))
        lines.append(escape_html(row["reasoning"]))

    strengths = row.get("strengths") or []
    risks = row.get("risks") or []
    if strengths:
        lines.append("")
        lines.append(bold("На користь:"))
        for point in strengths:
            lines.append(f"✅ {escape_html(point)}")
    if risks:
        lines.append("")
        lines.append(bold("Проти / ризики:"))
        for point in risks:
            lines.append(f"⚠️ {escape_html(point)}")

    lines.append("")
    lines.append("<i>ℹ️ Опис впливу чинників, не вказівка діяти.</i>")
    return "\n".join(lines)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    token, chat_id = resolve_telegram_credentials()

    conn = get_connection()
    try:
        rows = fetch_unnotified(conn, limit=args.limit)
        if not rows:
            logger.info("Нових фундаментальних аналізів немає — не надсилаємо")
            return

        # Окреме повідомлення на кожен актив — розгорнутий розбір не
        # вміщається батчем у ліміт Telegram (див. докстрінг модуля).
        # mark_notified ПО ОДНОМУ: якщо відправка впаде на пʼятому
        # активі, перші чотири лишаться позначеними й не підуть удруге.
        sent = 0
        for row in rows:
            send_telegram_message(
                token, chat_id, format_asset_message(row), parse_mode="HTML"
            )
            mark_notified(conn, "fundamental_analysis", [row["id"]])
            sent += 1
        logger.info("Надіслано в Telegram: %d аналізів (окремими повідомленнями)", sent)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
