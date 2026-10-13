#!/usr/bin/env python3
"""
Сповіщення в Telegram про нових кандидатів-новачків — активи, у яких
знайдено РУХ (угору або вниз), яких ще немає в наших списках
(`candidate_assets`, джерело — `analysis/discovery/`). Тільки
форматування готового результату, жодної аналітики
(reporting/CLAUDE.md).

Переписано 2026-10-05 разом із самим пошуком: кандидат тепер має КЛАС
активу (акція/крипта), НАПРЯМОК руху й детектор, що його знайшов —
повідомлення без цього читалося б як старе "ось вам тикер", хоча
головне тут саме напрямок ("падає на обсязі" і "зростає на обсязі" —
різні події). Напрямок описує РУХ, не вказівку діяти
(analysis/CLAUDE.md, "Заборонені формулювання").

Дедуп (2026-09-28, критичний фікс, той самий принцип що
news_notify.py): `notified_at IS NULL` — раніше скрипт завжди показував
ПОТОЧНИЙ список (останні 10 унікальних тикерів), тож той самий тикер
міг з'являтись у сповіщенні щодня, навіть якщо про нього нема нічого
нового. Тепер — лише тикери, знайдені/оновлені (`discovery/run_discovery.py`
UPSERT-ить один рядок на тикер НА ДЕНЬ) з часу останнього успішного
сповіщення.

Використання (після analysis/discovery/run_discovery.py):
    python candidates_notify.py
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
from config import DISCOVERY_MAX_ITEMS  # noqa: E402
from telegram_client import send_telegram_message  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Ключ для _common.composition_changed — дорівнює назві джоби.
NOTIFIER = "notify_candidates"


def fetch_unnotified_candidates(conn, limit: int = DISCOVERY_MAX_ITEMS) -> list[dict]:
    """Тикери, ще не надіслані в Telegram (`notified_at IS NULL`) —
    власний запит, не імпорт з analysis/ (reporting/CLAUDE.md:
    контейнерна незалежність)."""
    return fetch_dicts(
        conn,
        """
        SELECT id, ticker, company_name, reasoning, discovered_at,
               asset_class, direction, detector, score
        FROM candidate_assets
        WHERE notified_at IS NULL
        ORDER BY score DESC NULLS LAST, discovered_at DESC
        LIMIT %s
        """,
        (limit,),
    )


# Напрямок РУХУ (не рекомендація) — ті самі слова, що
# trading_list_notify.py:DIRECTION_PHRASE, щоб два списки читались
# однією мовою.
DIRECTION_PHRASE = {
    "up": "↑ рух угору",
    "down": "↓ рух униз",
    "neutral": "· без напрямку",
    "unclear": "· напрямок неясний",
}

CLASS_LABEL = {
    "crypto": "🪙 Крипта",
    "stock": "📈 Акції",
}

# ЯКИЙ детектор знайшов — користувачу це каже, наскільки сигнал
# самостійний (рух на біржі vs згадка в новинах).
DETECTOR_LABEL = {
    "crypto_movers": "рух на ф'ючерсах",
    "stock_movers": "рух ціни акції",
    "news_tickers": "новина + перевірена ціна",
}


def format_message(rows: list[dict]) -> str:
    if not rows:
        return "🆕 Нових кандидатів-новачків немає."

    lines = [
        bold(f"🆕 Новачки — знайдено рух у {len(rows)} активах"),
        "<i>Ще не в наших списках. Напрямок руху, не вказівка діяти.</i>",
        "",
    ]

    # Групування за класом активу: крипта й акції рухаються в різних
    # масштабах (8% за добу vs 6% за тиждень), і змішаний список
    # створював би хибне відчуття, що їх можна порівнювати напряму.
    for asset_class, label in CLASS_LABEL.items():
        group = [r for r in rows if r.get("asset_class") == asset_class]
        if not group:
            continue
        lines.append(bold(label))
        for row in group:
            name = row["company_name"] or row["ticker"]
            phrase = DIRECTION_PHRASE.get(row.get("direction"), "")
            detector = DETECTOR_LABEL.get(row.get("detector"), row.get("detector") or "")
            lines.append(f"{bold(row['ticker'])} — {escape_html(name)}")
            lines.append(f"{phrase} · <i>{escape_html(detector)}</i>")
            lines.append(escape_html(row["reasoning"]))
            lines.append("")

    # Клас, якого немає в CLASS_LABEL (з'явиться, коли детектори
    # розширяться на форекс/товари) — не губиться, іде в кінці.
    known = set(CLASS_LABEL)
    other = [r for r in rows if r.get("asset_class") not in known]
    for row in other:
        lines.append(bold(f"{row['ticker']} — {row['company_name'] or row['ticker']}"))
        lines.append(escape_html(row["reasoning"]))
        lines.append("")

    return "\n".join(lines).rstrip()


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    # Дефолт — із конфіга, не власна копія числа (живий прогін
    # 2026-10-05: пошук зберіг 12 кандидатів, а notify з ліміту 10
    # надіслав лише 10 — двоє щоразу "залипали" до наступного
    # прогону). Той самий принцип "одне джерело істини для порогів",
    # що й у решті агента.
    parser.add_argument("--limit", type=int, default=DISCOVERY_MAX_ITEMS)
    args = parser.parse_args()

    token, chat_id = resolve_telegram_credentials()

    conn = get_connection()
    try:
        rows = fetch_unnotified_candidates(conn, limit=args.limit)
        if not rows:
            logger.info("Нових кандидатів немає — сповіщення не надсилається")
            return

        # Живий привід (12 жовтня): акційна частина списку була
        # ІДЕНТИЧНОЮ тричі за добу — CCI/SBAC/MRNA/AMT/HUM/VST з тими
        # самими числами, бо `quotes_universe_refresh` оновлює ціни
        # раз на добу о 03:00. Крипта за той самий день змінювалась,
        # тож дедуп по складу прибирає саме дублі, не живі оновлення.
        tickers = [row["ticker"] for row in rows]
        if not composition_changed(conn, NOTIFIER, tickers):
            mark_notified(conn, "candidate_assets", [row["id"] for row in rows])
            logger.info(
                "Склад новачків не змінився (%d) — не надсилаємо", len(tickers),
            )
            return

        text = format_message(rows)
        send_telegram_message(token, chat_id, text, parse_mode="HTML")
        mark_notified(conn, "candidate_assets", [row["id"] for row in rows])
        remember_composition(conn, NOTIFIER, tickers)
        logger.info("Надіслано в Telegram: %d кандидатів", len(rows))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
