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

## З 2026-10-24: вердикт + короткі рядки

Аналіз кожного активу тепер робить DeepSeek, а Anthropic — один
ФІНАЛЬНИЙ ВЕРДИКТ поверх усіх (`fundamental_verdict`, рішення
користувача: "скоротити Anthropic без втрати якості"). Коли вердикт є,
йде ОДНЕ повідомлення: вердикт (головне, суперечності, на що зважати)
і під ним по рядку на актив. Розгорнутий розбір кожного активу лишається
в БД (його бачить `/ask`). Без вердикту (збій виклику, ручний прогін
одного активу) — як раніше, окреме повідомлення на актив, щоб
висновок не загубився.

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


def fetch_unnotified_verdict(conn):
    rows = fetch_dicts(
        conn,
        """
        SELECT id, verdict_date, summary, key_points, conflicts, watch, analysis_ids
        FROM fundamental_verdict
        WHERE notified_at IS NULL
        ORDER BY verdict_date DESC
        LIMIT 1
        """,
        (),
    )
    return rows[0] if rows else None


def fetch_verdict_analyses(conn, ids: list[int]) -> list[dict]:
    """Висновки, що пішли у вердикт — і нові, і "без змін", у тому
    порядку, в якому вони були у вході."""
    if not ids:
        return []
    rows = fetch_dicts(
        conn,
        """
        SELECT id, asset_id, asset_kind, company_name, direction, confidence,
               summary, created_at, notified_at
        FROM fundamental_analysis
        WHERE id = ANY(%s)
        """,
        (list(ids),),
    )
    order = {analysis_id: i for i, analysis_id in enumerate(ids)}
    return sorted(rows, key=lambda r: order.get(r["id"], len(order)))


def format_verdict_message(verdict: dict, analyses: list[dict]) -> str:
    """Одне повідомлення: вердикт і під ним по рядку на актив. Рядок
    активу без змін входів позначається датою його висновку."""
    lines = [
        bold(f"🧭 Фундаментальний вердикт · {verdict['verdict_date']}"),
        "",
        escape_html(verdict["summary"]),
    ]
    for title, key, mark in (
        ("Головне:", "key_points", "•"),
        ("Суперечності:", "conflicts", "⚖️"),
        ("На що зважати:", "watch", "👁"),
    ):
        items = verdict.get(key) or []
        if items:
            lines.append("")
            lines.append(bold(title))
            lines.extend(f"{mark} {escape_html(item)}" for item in items)

    if analyses:
        lines.append("")
        lines.append(bold("По активах:"))
        for row in analyses:
            name = row.get("company_name") or row["asset_id"]
            emoji = DIRECTION_EMOJI.get(row["direction"], "❓")
            stale = ""
            if row.get("notified_at") is not None:
                created = row["created_at"]
                stale = f" <i>(без змін з {created:%d.%m})</i>" if hasattr(created, "strftime") else ""
            lines.append(
                f"{emoji} {bold(name)} · {float(row['confidence']):.2f}{stale} — "
                f"{escape_html(row['summary'])}"
            )

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
        verdict = fetch_unnotified_verdict(conn)
        if verdict is not None:
            analyses = fetch_verdict_analyses(conn, verdict.get("analysis_ids") or [])
            send_telegram_message(
                token, chat_id, format_verdict_message(verdict, analyses), parse_mode="HTML"
            )
            mark_notified(conn, "fundamental_verdict", [verdict["id"]])
            # Висновки, що вже показані рядком під вердиктом, окремим
            # повідомленням не йдуть.
            fresh = [row["id"] for row in analyses if row.get("notified_at") is None]
            mark_notified(conn, "fundamental_analysis", fresh)
            logger.info(
                "Надіслано в Telegram: вердикт + %d активів (%d нових висновків)",
                len(analyses), len(fresh),
            )

        rows = fetch_unnotified(conn, limit=args.limit)
        if not rows:
            if verdict is None:
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
