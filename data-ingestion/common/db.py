"""
Шар роботи з БД для data-ingestion.

Свідомо тонкий: жодної інтерпретації значень, тільки збереження й
читання "останнього відомого значення" (потрібне reporting/ для
Telegram-сповіщення в Фазі 0). Порівняння факт/очікування — це вже
analysis/, не сюди.
"""

import json
import logging
import os
from contextlib import contextmanager
from decimal import Decimal
from typing import Optional

import psycopg2
import psycopg2.extras

from common.adapter import NormalizedRecord

logger = logging.getLogger(__name__)


def get_connection():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


@contextmanager
def _cursor(conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield cur
    finally:
        cur.close()


def _latest_stored(cur, source: str, metric_id: str, observed_at) -> Optional[dict]:
    cur.execute(
        """
        SELECT value, revision
        FROM raw_observations
        WHERE source = %s AND metric_id = %s AND observed_at = %s
        ORDER BY revision DESC
        LIMIT 1
        """,
        (source, metric_id, observed_at),
    )
    return cur.fetchone()


def decide_revision(latest: Optional[dict], new_value: Decimal) -> Optional[int]:
    """Чиста логіка визначення номера ревізії — винесена окремо від SQL,
    щоб тестувалась без реальної БД.

    latest — результат _latest_stored() (dict з "value"/"revision") або None.
    Повертає номер ревізії для запису, або None якщо значення не змінилось
    і писати нічого не треба.
    """
    if latest is None:
        return 1
    if Decimal(latest["value"]) == new_value:
        return None
    return latest["revision"] + 1


def insert_observation(conn, record: NormalizedRecord) -> Optional[int]:
    """Append-only запис одного значення з визначенням revision.

    - якщо для (source, metric_id, observed_at) ще нічого немає → revision 1
    - якщо є, але значення відрізняється від останнього → нова ревізія (+1)
    - якщо є і значення те саме → нічого не пишемо (ідемпотентність
      повторних запусків збору), повертаємо None

    Повертає номер записаної ревізії, або None якщо запис пропущено.
    """
    with _cursor(conn) as cur:
        latest = _latest_stored(cur, record.source, record.metric_id, record.observed_at)
        revision = decide_revision(latest, record.value)

        if revision is None:
            logger.debug(
                "Без змін, пропускаємо: %s/%s за %s",
                record.source, record.metric_id, record.observed_at,
            )
            return None

        if latest is not None:
            logger.info(
                "Виявлено ревізію %s/%s за %s: %s → %s (revision %d)",
                record.source, record.metric_id, record.observed_at,
                latest["value"], record.value, revision,
            )

        cur.execute(
            """
            INSERT INTO raw_observations
                (source, metric_id, value, observed_at, fetched_at, revision, raw_payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, metric_id, observed_at, revision) DO NOTHING
            """,
            (
                record.source,
                record.metric_id,
                record.value,
                record.observed_at,
                record.fetched_at,
                revision,
                json.dumps(record.raw_payload, default=str) if record.raw_payload else None,
            ),
        )
    conn.commit()
    return revision


def insert_observations(conn, records: list[NormalizedRecord]) -> int:
    """Записує список нормалізованих записів, повертає кількість
    фактично вставлених (нових/змінених) значень."""
    inserted = 0
    for record in records:
        if insert_observation(conn, record) is not None:
            inserted += 1
    return inserted


def fetch_latest(conn, source: str, metric_id: str) -> Optional[dict]:
    """Останнє (за observed_at, потім revision) значення показника —
    те, що показує reporting/telegram_notify.py."""
    with _cursor(conn) as cur:
        cur.execute(
            """
            SELECT source, metric_id, value, observed_at, fetched_at, revision
            FROM raw_observations
            WHERE source = %s AND metric_id = %s
            ORDER BY observed_at DESC, revision DESC
            LIMIT 1
            """,
            (source, metric_id),
        )
        return cur.fetchone()


def fetch_recent(conn, source: str, metric_id: str, limit: int) -> list[dict]:
    """Останні `limit` спостережень (найновіше перше), без шуму старих
    ревізій — читає з `v_observations_latest_revision` (для кожної дати
    лишається тільки останнє відоме значення). Потрібне analysis/expectations/
    для обчислення m/m %, y/y % чи діапазонних змін: сире значення в
    raw_observations часто це РІВЕНЬ показника, а не та величина, яку
    дає ринковий прогноз."""
    with _cursor(conn) as cur:
        cur.execute(
            """
            SELECT source, metric_id, value, observed_at, fetched_at, revision
            FROM v_observations_latest_revision
            WHERE source = %s AND metric_id = %s
            ORDER BY observed_at DESC
            LIMIT %s
            """,
            (source, metric_id, limit),
        )
        return cur.fetchall()
