"""
Шар роботи з БД для новин — паралельний до common/db.py (raw_news
має інакшу структуру дедуплікації: (source, external_id), без
revision, докладніше docs/decisions.md, 2026-09-25).

Свіжість (2026-09-28, рішення користувача): raw_news — ВИНЯТОК із
critical rule 6 кореневого CLAUDE.md ("ніколи не видаляти сирі
дані") — обґрунтування правила ("історичні дані — те, на чому
тримається якість майбутніх прогнозів") стосується макропоказників
(raw_observations), не новинних статей: стаття тижневої давності не
покращує якість прогнозу, лише захаращує "важливі новини" застарілим
контентом. Тому й ЗБІР (MAX_ARTICLE_AGE_HOURS нижче), і ЗБЕРІГАННЯ
(data-ingestion/prune_raw_news.py, 48г) тут навмисно НЕ append-only-
вічні, на відміну від raw_observations. Деталі — docs/decisions.md.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from common.news_adapter import NewsRecord

logger = logging.getLogger(__name__)

# Не приймати статті старші за це — джерело (GDELT/RSS) іноді віддає
# передрук/архівну статтю з давнім published_at попри "свіжий" запит.
# Єдина крапка застосування для ВСІХ колекторів (run_collect_news.py —
# GDELT, run_collect_rss.py — RSS, collect_stock_news.py — GDELT) —
# щоб не дублювати перевірку в кожному окремо.
MAX_ARTICLE_AGE_HOURS = 24

# 2026-09-29, рішення користувача: fed_rss/ecb_rss/boj_rss публікують
# рідко (кілька разів на тиждень, не щодня) — суворі 24г/48г вікна
# систематично губили б їх ПОВНІСТЮ (живо підтверджено: fed_rss живий,
# повернув 20 реальних записів, усі відсіяні як "застарілі" — 0 рядків
# у raw_news взагалі), попри те, що це найавторитетніші джерела з усіх
# 9 RSS-фідів. Довше вікно — ЛИШЕ для цих трьох, решта (6 редакційних
# RSS-фідів + GDELT) лишається на 24г/48г.
EXTENDED_FRESHNESS_SOURCES = frozenset({"fed_rss", "ecb_rss", "boj_rss"})
EXTENDED_MAX_ARTICLE_AGE_HOURS = 24 * 7   # 7 днів
EXTENDED_RETENTION_HOURS = 24 * 7         # те саме вікно й для зберігання —
# інакше стаття, щойно прийнята на збірці (до 7 днів), одразу підпала
# б під звичайний 48г prune_stale_news() і зникла б, не діставшись
# консолідації.


def insert_news(conn, record: NewsRecord) -> bool:
    """Append-only запис однієї новини (у межах свіжості — див.
    MAX_ARTICLE_AGE_HOURS/insert_news_batch()). Ідемпотентно: повторний
    збір тієї самої статті (той самий source+external_id) нічого не
    змінює.

    Повертає True, якщо запис фактично вставлено (нова стаття).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_news
                (source, external_id, stream, title, url, published_at, fetched_at, raw_payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, external_id) DO NOTHING
            """,
            (
                record.source,
                record.external_id,
                record.stream,
                record.title,
                record.url,
                record.published_at,
                record.fetched_at,
                json.dumps(record.raw_payload, default=str) if record.raw_payload else None,
            ),
        )
        inserted = cur.rowcount == 1
    conn.commit()
    return inserted


def _max_age_for(source: str, max_age_hours: Optional[int]) -> int:
    """Явно передане `max_age_hours` завжди перекриває все; інакше —
    вікно САМЕ ДЛЯ ЦЬОГО source (EXTENDED_FRESHNESS_SOURCES) чи
    дефолт. Кожен record сам несе свій `source`, тому виклику
    (run_collect_rss.py/run_collect_news.py) не треба нічого знати
    про виняток — правило живе в одному місці."""
    if max_age_hours is not None:
        return max_age_hours
    return EXTENDED_MAX_ARTICLE_AGE_HOURS if source in EXTENDED_FRESHNESS_SOURCES else MAX_ARTICLE_AGE_HOURS


def insert_news_batch(conn, records: list[NewsRecord], max_age_hours: Optional[int] = None) -> int:
    """Записує список новин, СПОЧАТКУ відсіявши застарілі (докстрінг
    модуля: MAX_ARTICLE_AGE_HOURS дефолт, довше для
    EXTENDED_FRESHNESS_SOURCES — за `record.source`, не одне число на
    весь батч, бо один виклик рідко буває однорідним за джерелом лише
    випадково). `max_age_hours` — явний виняток на весь виклик, коли
    він таки потрібен (тести/ручна перевірка). Повертає кількість
    фактично нових статей (застарілі в цей рахунок не входять)."""
    now = datetime.now(timezone.utc)
    fresh, stale = [], []
    for record in records:
        cutoff = now - timedelta(hours=_max_age_for(record.source, max_age_hours))
        (fresh if record.published_at >= cutoff else stale).append(record)

    if stale:
        logger.info(
            "Відсіяно %d застарілих статей, напр. %r (%s, джерело %s)",
            len(stale), stale[0].title, stale[0].published_at, stale[0].source,
        )

    return sum(1 for record in fresh if insert_news(conn, record))


# Скільки тримати статтю в БД ПІСЛЯ вставки, перш ніж прибрати
# (docstring модуля — свідомий виняток із rule 6 для raw_news, не
# raw_observations). Довше за MAX_ARTICLE_AGE_HOURS (24) — новина
# лишається доступною для аналізу/дедупу ще добу після того, як
# перестає бути "свіжою" для ЗБОРУ.
RETENTION_HOURS = 48


def prune_stale_news(
    conn, max_age_hours: int = RETENTION_HOURS, extended_max_age_hours: int = EXTENDED_RETENTION_HOURS
) -> int:
    """DELETE статей, старших за `max_age_hours` (за published_at) —
    orchestration/jobs.py:_prune_raw_news() викликає це щодня. Не
    append-only (свідомий виняток, докстрінг модуля вище). Джерела з
    EXTENDED_FRESHNESS_SOURCES отримують довше вікно
    (`extended_max_age_hours`) — інакше стаття, щойно прийнята на
    збірці (до 7 днів для цих трьох), одразу підпала б під звичайний
    48г-prune і зникла б, не діставшись консолідації. Повертає
    кількість видалених рядків."""
    sources = ", ".join(f"'{s}'" for s in EXTENDED_FRESHNESS_SOURCES)  # фіксований набір у коді, не ввід
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM raw_news
            WHERE (source NOT IN ({sources}) AND published_at < now() - (%s || ' hours')::interval)
               OR (source IN ({sources}) AND published_at < now() - (%s || ' hours')::interval)
            """,
            (max_age_hours, extended_max_age_hours),
        )
        deleted = cur.rowcount
    conn.commit()
    return deleted
