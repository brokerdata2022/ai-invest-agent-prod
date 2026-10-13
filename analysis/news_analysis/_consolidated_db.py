"""
Тонкий SQL-шар для news_consolidated (analysis/news_analysis/consolidate.py)
— той самий принцип, що news_analysis/_db.py: SQL без інтерпретації
значень, логіка (промпт/парсинг/об'єднання) лишається в consolidate.py.
"""

import json
from difflib import SequenceMatcher
from typing import Optional

# Наскільки схожими мають бути ДВА summary, щоб рахувати їх тією самою
# історією між РІЗНИМИ прогонами консолідації (docs/decisions.md,
# 2026-09-28: та сама новина про мінфіна Британії потрапила в
# news_consolidated ДВІЧІ — окремі прогони, кожен зі своєю
# кластеризацією "усередині себе", не бачать один одного). Той самий
# принцип і поріг, що aggregate.py:_title_similarity — тут порівнюємо
# вже готовий (перекладений) summary, не сирий заголовок.
# Поріг схожості МІЖ прогонами — з `config.py` (один файл для
# ручного редагування в продакшені, рішення користувача 2026-10-02).
from config import CROSS_RUN_SIMILARITY_THRESHOLD  # noqa: E402,F401


def fetch_unconsolidated_raw_news(
    conn, stream: Optional[str] = None, max_age_days: int = 3, limit: int = 60
) -> list[dict]:
    """Сирі статті (raw_news), ще НЕ враховані в жодному прогоні
    консолідації (`consolidated_at IS NULL`) — за published_at (коли
    стаття вийшла), у межах max_age_days. limit — стеля на розмір
    одного LLM-промпту, не критерій відбору."""
    query = """
        SELECT id, source, stream, title, url, published_at, raw_payload
        FROM raw_news
        WHERE consolidated_at IS NULL
          AND published_at >= now() - (%s || ' days')::interval
    """
    params: list = [max_age_days]
    if stream is not None:
        query += " AND stream = %s"
        params.append(stream)
    query += " ORDER BY published_at DESC LIMIT %s"
    params.append(limit)

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def mark_consolidated(conn, raw_news_ids: list[int]) -> None:
    """Позначає СИРІ статті як враховані в консолідації (незалежно від
    того, чи вони потрапили у фінальний вивід, чи DeepSeek їх
    відкинув як шум) — щоб той самий прогін (кілька разів на добу) не
    пережовував ту саму статтю знову."""
    if not raw_news_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE raw_news SET consolidated_at = now() WHERE id = ANY(%s)",
            (raw_news_ids,),
        )
    conn.commit()


def fetch_recent_summaries(conn, stream: str, max_age_days: int = 3) -> list[str]:
    """Summary вже збережених консолідованих новин того самого потоку
    за останні max_age_days (незалежно від notified_at — навіть уже
    надіслана вчора новина не повинна повторно з'явитись сьогодні як
    "нова", якщо джерела просто передрукували той самий факт)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT summary FROM news_consolidated
            WHERE stream = %s AND created_at >= now() - (%s || ' days')::interval
            """,
            (stream, max_age_days),
        )
        return [row[0] for row in cur.fetchall()]


def is_duplicate_of_recent(summary: str, recent_summaries: list[str]) -> bool:
    """Чиста функція (тестується без БД) — чи summary достатньо схожий
    на щось, уже збережене цього потоку за вікно, щоб вважати те самою
    історією з ІНШОГО прогону консолідації (у межах ОДНОГО прогону
    дублі й так об'єднує сам LLM-промпт, source_indices)."""
    return any(
        SequenceMatcher(None, summary, existing).ratio() >= CROSS_RUN_SIMILARITY_THRESHOLD
        for existing in recent_summaries
    )


def fetch_mergeable(conn, stream: str, window_hours: int = 24) -> list[dict]:
    """Ще НЕ надіслані консолідовані записи потоку за вікно —
    кандидати для merge_similar.py (перевірка на семантичні дублі МІЖ
    прогонами consolidate.py). Сортує за created_at, щоб найновіше
    (найімовірніше вже мало шанс дублювати щойно збережене) було
    поруч — не критично для коректності, лише зручність читання логів."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, asset_id, summary, direction, confidence,
                   source_count, source_raw_news_ids, source_urls
            FROM news_consolidated
            WHERE stream = %s
              AND notified_at IS NULL
              AND created_at >= now() - (%s || ' hours')::interval
            ORDER BY created_at
            """,
            (stream, window_hours),
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def merge_consolidated_rows(conn, group_rows: list[dict]) -> int:
    """Об'єднує group_rows (≥2, той самий формат, що fetch_mergeable())
    в ОДИН запис — лишає той, що мав найвищий source_count (уже
    найбільш підтверджений незалежно), НАКОПИЧУЄ джерела решти в нього
    (сума source_count, об'єднання source_raw_news_ids/source_urls, БЕЗ
    дублів), видаляє решту. Повертає кількість ВИДАЛЕНИХ рядків (для
    підрахунку в merge_similar.py:main())."""
    primary = max(group_rows, key=lambda r: r["source_count"])
    others = [r for r in group_rows if r["id"] != primary["id"]]

    merged_raw_news_ids = list(dict.fromkeys(
        rid for r in group_rows for rid in r["source_raw_news_ids"]
    ))
    merged_urls = list(dict.fromkeys(
        url for r in group_rows for url in r["source_urls"]
    ))

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE news_consolidated
            SET source_count = %s, source_raw_news_ids = %s, source_urls = %s
            WHERE id = %s
            """,
            (len(merged_raw_news_ids), json.dumps(merged_raw_news_ids), json.dumps(merged_urls), primary["id"]),
        )
        if others:
            cur.execute(
                "DELETE FROM news_consolidated WHERE id = ANY(%s)",
                ([r["id"] for r in others],),
            )
    conn.commit()
    return len(others)


def save_consolidated_item(
    conn,
    stream: str,
    asset_id: Optional[str],
    summary: str,
    direction: str,
    confidence: float,
    reasoning: str,
    source_raw_news_ids: list[int],
    source_urls: list[str],
    llm_call_id: int,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO news_consolidated
                (stream, asset_id, summary, direction, confidence, reasoning,
                 source_count, source_raw_news_ids, source_urls, llm_call_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                stream,
                asset_id,
                summary,
                direction,
                confidence,
                reasoning,
                len(source_raw_news_ids),
                json.dumps(source_raw_news_ids),
                json.dumps(source_urls),
                llm_call_id,
            ),
        )
        item_id = cur.fetchone()[0]
    conn.commit()
    return item_id
