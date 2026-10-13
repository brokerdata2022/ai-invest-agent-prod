#!/usr/bin/env python3
"""
Сповіщення в Telegram про КІЛЬКА справді пріоритетних новин — вже
відфільтрованих, об'єднаних (дублікати з різних сайтів/мов в один
запис) і перекладених українською `analysis/news_analysis/consolidate.py`
(`news_consolidated`). Тільки форматування готового результату,
жодної інтерпретації тут (reporting/CLAUDE.md).

Без `--stream` — ОДНЕ сповіщення з усіх зібраних потоків разом
(рішення користувача 2026-09-28: "важливі новини мають бути не тільки
з watchlist, а зі всіх зібраних новин").

Три живих баги підряд 2026-09-28, і головний урок з них — `--limit` це
НЕ "стеля перед тим, як щось піде не так", а РЕАЛЬНИЙ розмір
"пріоритетного списку" (пряма вимога користувача: "ШІ має з них
вибрати тільки важливі пріоритетні і скинути мені", не "усе, що вище
довільного порогу"):
1. Одне об'єднане повідомлення з 20 новин — 5739 символів → Telegram
   `400 Bad Request` (ліміт 4096).
2. Фікс "одна новина — одне повідомлення" це виправив, але заспамив
   користувача десятками повідомлень підряд за один прогін.
3. Причина — `DEFAULT_MIN_CONFIDENCE=0.6` + `--limit=20` пропускали
   ледь не все "достатньо релевантне" як "важливе". Тепер
   `--limit` дефолтно 8 (реальний розмір пріоритетного списку, не
   бэклог) + вищий поріг `confidence`, і `batch_messages()` пакує ці
   кілька новин у 1-2 повідомлення (не 8 окремих) — у межах
   `MAX_MESSAGE_CHARS`.

Використання (після analysis/news_analysis/consolidate.py):
    python news_notify.py
    python news_notify.py --stream watchlist
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
    link,
    mark_notified,
    resolve_telegram_credentials,
)
from common.db import get_connection  # noqa: E402
from telegram_client import send_telegram_message  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Реальний розмір "пріоритетного списку" за один прогін (не бэклог,
# не "усе достатньо релевантне") — docstring модуля.
#
# Поріг РІЗНИЙ per stream (2026-09-28, живо виявлено): watchlist/general
# прив'язані до конкретного активу — на 0.6-0.7 там сидить реальний
# ринковий контент (Bitcoin/срібло/дохідності/ETH-кити). geopolitical —
# ширші world-новини без прив'язки до активу, DeepSeek там регулярно
# ставить 0.6-0.7 навіть подіям БЕЗ чіткого економічного наслідку
# (ураган, військові удари) — самої "геополітичної значущості" йому
# досить для середньої впевненості, а не тільки справжнього ринкового
# зв'язку. Тому geopolitical лишається на вищому порозі.
MIN_CONFIDENCE_BY_STREAM: dict[str, float] = {
    "watchlist": 0.6,
    "general": 0.6,
    "geopolitical": 0.75,
}
DEFAULT_MIN_CONFIDENCE = 0.6  # використовується лише коли --stream не задано (усі потоки разом)
DEFAULT_LIMIT = 8

# Гарантований мінімум місць для watchlist (2026-09-28, живий фідбек
# користувача: "жодної новини про крипту, жодної зі списку акцій" —
# generic `general`-контент (компанія самокатів, downgrade без цифр)
# випадково мав вищий confidence і забивав усі 8 місць, хоча
# watchlist — це САМЕ те, за чим стежить користувач (crypto/метали/
# нафта/FX + акції зі скринінгу через collect_stock_news.py). Плаский
# ORDER BY confidence DESC по всіх потоках разом не гарантує
# представленості жодного конкретного потоку.
WATCHLIST_MIN_SLOTS = 3

# Запас під реальний ліміт Telegram (4096) для batch_messages().
MAX_MESSAGE_CHARS = 3500


def fetch_top_unnotified(
    conn, stream: str = None, min_confidence: float = None, limit: int = DEFAULT_LIMIT
) -> list[dict]:
    """ТОП-`limit` найважливіших ще НЕ надісланих
    (`notified_at IS NULL`) консолідованих новин — `limit` тут реальний
    розмір списку "що показати", не стеля-запобіжник.

    `min_confidence=None` (дефолт) — використовує РІЗНИЙ поріг per
    stream (`MIN_CONFIDENCE_BY_STREAM`), а не один глобальний. Явне
    число перекриває це для всіх потоків одразу (--min-confidence)."""
    query = """
        SELECT id, stream, asset_id, summary, direction, confidence,
               source_count, source_urls, created_at
        FROM news_consolidated
        WHERE notified_at IS NULL
    """
    params: list = []

    if min_confidence is not None:
        query += " AND confidence >= %s"
        params.append(min_confidence)
    elif stream is not None:
        query += " AND confidence >= %s"
        params.append(MIN_CONFIDENCE_BY_STREAM.get(stream, DEFAULT_MIN_CONFIDENCE))
    else:
        # Усі потоки разом, без --min-confidence — поріг кожного рядка
        # залежить від ЙОГО ВЛАСНОГО stream (SQL CASE), не одне число
        # на все (docstring модуля: geopolitical потребує вищого бар'єру).
        query += """ AND confidence >= CASE stream
            WHEN 'watchlist' THEN %s
            WHEN 'general' THEN %s
            WHEN 'geopolitical' THEN %s
            ELSE %s
        END"""
        params.extend([
            MIN_CONFIDENCE_BY_STREAM["watchlist"],
            MIN_CONFIDENCE_BY_STREAM["general"],
            MIN_CONFIDENCE_BY_STREAM["geopolitical"],
            DEFAULT_MIN_CONFIDENCE,
        ])

    if stream is not None:
        query += " AND stream = %s"
        params.append(stream)
    # Пріоритет за source_count (МЕХАНІЧНИЙ факт — скільки незалежних
    # джерел підтвердили, `analysis/news_analysis/consolidate.py`), не
    # за confidence (2026-09-28, рішення користувача: "проблема в
    # оцінках, виправляй оцінку, не поріг"). confidence лишається лише
    # воротами релевантності (WHERE вище), не мірилом важливості —
    # інакше один SEO-сайт з роздутою LLM-оцінкою знову витіснив би
    # подію, підтверджену 5 незалежними виданнями.
    query += " ORDER BY source_count DESC, confidence DESC, created_at DESC LIMIT %s"
    params.append(limit)

    return fetch_dicts(conn, query, tuple(params))


def fetch_prioritized_unnotified(
    conn, min_confidence: float = None, limit: int = DEFAULT_LIMIT,
    watchlist_min_slots: int = WATCHLIST_MIN_SLOTS,
) -> list[dict]:
    """Те саме, що fetch_top_unnotified(stream=None), АЛЕ з гарантованим
    мінімумом місць для watchlist (docstring константи вище) — інакше
    generic `general`-контент може випадково мати вищий confidence і
    забити всі місця, залишивши крипту/watchlist-активи взагалі поза
    вибіркою. Спершу топ watchlist (до watchlist_min_slots), потім
    решта місць — найкраще з УСІХ потоків, виключаючи вже обрані id."""
    watchlist_items = fetch_top_unnotified(
        conn, stream="watchlist", min_confidence=min_confidence, limit=watchlist_min_slots
    )
    if len(watchlist_items) >= limit:
        return watchlist_items[:limit]

    remaining = fetch_top_unnotified(conn, stream=None, min_confidence=min_confidence, limit=limit)
    chosen_ids = {row["id"] for row in watchlist_items}
    fill = [row for row in remaining if row["id"] not in chosen_ids]

    combined = watchlist_items + fill[: limit - len(watchlist_items)]
    combined.sort(key=lambda row: (row["source_count"], row["confidence"]), reverse=True)
    return combined


def format_item(row: dict) -> str:
    emoji = DIRECTION_EMOJI.get(row["direction"], "❓")
    asset = row["asset_id"] or "—"
    source_note = f" ({row['source_count']} джерел)" if row["source_count"] > 1 else ""
    header = f"{emoji} {bold(f'[{asset}]')} {escape_html(row['summary'])}{source_note}"
    lines = [header]
    # Клікабельні посилання замість голих URL — не роздувати повідомлення,
    # якщо джерел багато (той самий ліміт [:3], що був).
    lines.extend(
        f"🔗 {link(f'Джерело {i}', url)}" for i, url in enumerate(row["source_urls"][:3], start=1)
    )
    return "\n".join(lines)


def batch_messages(rows: list[dict], max_chars: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Пакує новини по кілька в одне повідомлення, поки влазить у
    `max_chars` — уникає і "одне гігантське" (Telegram 400), і "N
    окремих" (спам) крайнощів."""
    if not rows:
        return []

    header = bold(f"📰 Важливі новини ({len(rows)}):")
    messages: list[str] = []
    current = [header]
    current_len = len(header)

    for row in rows:
        item = format_item(row)
        added_len = len(item) + 2  # +2 за "\n\n"-роздільник
        if current_len + added_len > max_chars and len(current) > 1:
            messages.append("\n\n".join(current))
            current = [header]
            current_len = len(header)
        current.append(item)
        current_len += added_len

    messages.append("\n\n".join(current))
    return messages


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", choices=["watchlist", "general", "geopolitical"], default=None)
    parser.add_argument(
        "--min-confidence", type=float, default=None,
        help="перекрити поріг для ВСІХ потоків одразу (за замовчуванням — різний per stream, MIN_CONFIDENCE_BY_STREAM)",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="скільки пріоритетних новин показати")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="друкує повідомлення в консоль замість Telegram, НЕ позначає notified_at "
             "(для перевірки якості перед реальною відправкою — rule: ніколи не тестувати на живому чаті)",
    )
    args = parser.parse_args()

    conn = get_connection()
    try:
        if args.stream is None:
            rows = fetch_prioritized_unnotified(conn, min_confidence=args.min_confidence, limit=args.limit)
        else:
            rows = fetch_top_unnotified(
                conn, stream=args.stream, min_confidence=args.min_confidence, limit=args.limit
            )
        if not rows:
            logger.info("Нових важливих новин немає — сповіщення не надсилається")
            return

        messages = batch_messages(rows)

        if args.dry_run:
            for i, text in enumerate(messages, start=1):
                print(f"\n=== Повідомлення {i}/{len(messages)} ===\n{text}")
            logger.info("[dry-run] %d новин, %d повідомлень — НЕ надіслано, notified_at не змінено", len(rows), len(messages))
            return

        token, chat_id = resolve_telegram_credentials()
        for text in messages:
            send_telegram_message(token, chat_id, text, parse_mode="HTML")
        mark_notified(conn, "news_consolidated", [row["id"] for row in rows])

        logger.info("Надіслано в Telegram: %d новин, %d повідомлень", len(rows), len(messages))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
