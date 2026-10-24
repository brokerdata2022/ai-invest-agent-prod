#!/usr/bin/env python3
"""
Надсилає в Telegram фундаментальний LLM-аналіз акцій
(`fundamental_analysis` — `analysis/fundamental/`).

Не дублює `screening_notify.py`: там детермінований результат воронки
Tier A/B/C (тикер/назва/зміна ціни), тут — ІНТЕРПРЕТАЦІЯ: для акцій
звітності, для активів списку обраних — їхніх власних чинників
(золото: реальні ставки й долар; USD/JPY: різниця ставок ФРС/БОЯ).

## До 2026-10-24: одне повідомлення НА АКТИВ (тепер — `format_asset_message` для /ask)

Рішення користувача 2026-10-04: "розгорнутий аналіз по активах із
списку вибраних... користувачу надсилати висновок цього аналізу".
Розгорнутий розбір по чинниках — це ~1200 символів на актив; 11
активів в одному повідомленні впираються в ліміт Telegram 4096
(HTTP 400). Той самий урок, що вже був із `synthesis_notify.py`
(docs/decisions.md, 2026-10-02).

## З 2026-10-24: лише вердикт; розгорнутий розбір — через /ask

Аналіз кожного активу тепер робить DeepSeek, а Anthropic — один
ФІНАЛЬНИЙ ВЕРДИКТ поверх усіх (`fundamental_verdict`). Автоматично йде
ОДНЕ повідомлення: вердикт (головне, суперечності, на що зважати) і
під ним по рядку на актив. Розгорнутий розбір активу (по чинниках, за
і проти) — на запит: `/ask повний аналіз золота`
(`analysis/assistant/full_analysis.py`, той самий `format_asset_message`
нижче). Рішення користувача того ж дня, після того як перша ручна
відправка вивалила в чат 20 старих розборів із черги.

Без вердикту (збій виклику) — той самий список рядків без вердикту.
Після надсилання ВСЯ черга ненадісланих висновків позначається
обробленою: розгорнуті розбори автоматично не йдуть, тож їй нічого
чекати.

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
    DISPLAY_TIMEZONE,
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


def fetch_today_unnotified(conn) -> list[dict]:
    """Сьогоднішні ненадіслані висновки — для повідомлення без вердикту
    (вердикт не вдався): рядки по активах усе одно мають дійти."""
    return fetch_dicts(
        conn,
        """
        SELECT id, asset_id, asset_kind, company_name, direction, confidence,
               summary, created_at, notified_at
        FROM fundamental_analysis
        WHERE notified_at IS NULL
          AND (created_at AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date
        ORDER BY asset_kind DESC, created_at
        """,
        (DISPLAY_TIMEZONE, DISPLAY_TIMEZONE),
    )


def mark_all_unnotified(conn) -> int:
    """Позначити ВСЮ чергу ненадісланих висновків як оброблену.

    Розгорнуті розбори автоматично більше не надсилаються (рішення
    користувача 2026-10-24: "лишаємо загальний аналіз, розбір по
    чинниках — через /ask"), тож черга нічого не чекає. Заодно
    прибирається накопичене до цієї зміни: старе надсилання брало 10
    найновіших із 16 щоденних висновків, і 6 щодня лишались у черзі —
    на 24.10 їх було 54 у продакшені."""
    with conn.cursor() as cur:
        cur.execute("UPDATE fundamental_analysis SET notified_at = now() WHERE notified_at IS NULL")
        count = cur.rowcount
    conn.commit()
    return count


def format_list_message(rows: list[dict]) -> str:
    """Без вердикту (збій виклику): той самий список рядків по активах,
    щоб висновки дня не загубились."""
    lines = [bold("📊 Фундаментальний аналіз — висновки по активах"), ""]
    for row in rows:
        name = row.get("company_name") or row["asset_id"]
        emoji = DIRECTION_EMOJI.get(row["direction"], "❓")
        lines.append(
            f"{emoji} {bold(name)} · {float(row['confidence']):.2f} — {escape_html(row['summary'])}"
        )
    lines.append("")
    lines.append("<i>Загального вердикту сьогодні немає. Повний розбір активу — "
                 "/ask «повний аналіз &lt;актив&gt;».</i>")
    return "\n".join(lines)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="надрукувати, не надсилати і не позначати")
    args = parser.parse_args()

    conn = get_connection()
    try:
        verdict = fetch_unnotified_verdict(conn)
        if verdict is not None:
            analyses = fetch_verdict_analyses(conn, verdict.get("analysis_ids") or [])
            text = format_verdict_message(verdict, analyses)
        else:
            analyses = fetch_today_unnotified(conn)
            text = format_list_message(analyses) if analyses else None

        if args.dry_run:
            print(text or "Нового вердикту й сьогоднішніх висновків немає — нічого не надсилали б")
            return

        if text is not None:
            token, chat_id = resolve_telegram_credentials()
            send_telegram_message(token, chat_id, text, parse_mode="HTML")
            if verdict is not None:
                mark_notified(conn, "fundamental_verdict", [verdict["id"]])
            logger.info(
                "Надіслано в Telegram: %s, %d активів",
                "вердикт" if verdict is not None else "список без вердикту", len(analyses),
            )
        else:
            logger.info("Нового вердикту й сьогоднішніх висновків немає — не надсилаємо")

        skipped = mark_all_unnotified(conn)
        if skipped:
            logger.info("Черга висновків позначена обробленою: %d (розгорнуто — лише через /ask)", skipped)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
