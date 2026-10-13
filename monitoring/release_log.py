"""
release_log — I/O + чиста логіка для двофазного циклу моніторингу:

1. **Seed (наперед, `monitoring/refresh_calendar.py`, тижнева
   періодичність):** для кожного metric_id заводимо 'pending' рядок
   під НАЙБЛИЖЧУ ще не минулу дату релізу (`release_calendar.py:
   next_scheduled_release()`), збагачену часом/impact/forecast з
   `economic_calendar.py`, коли є збіг. `should_seed_new_cycle()` —
   чиста логіка "чи цей цикл уже заведено".
2. **Check (щохвилини, `monitoring/check_releases.py`):** дивимось на
   вже заведені 'pending' рядки, чий scheduled_at уже настав — і для
   них АКТИВНО йдемо забирати нові дані, щохвилини, доки не з'являться
   (або назавжди, якщо джерело затримало публікацію — append-only
   дедуп робить повтор безпечним). `is_past_buffer()` — чиста логіка
   "чи вже час пробувати".

Той самий підхід, що common/db.py (data-ingestion): чиста логіка
рішення винесена окремо від SQL, тестується без реальної БД.

monitoring лише ДЕТЕКТУЄ (`monitoring/CLAUDE.md`) — тому статус тут
рухається лише `pending → detected`. Перехід у `processed` — робота
analysis/ (коли аналіз фактично використав ці дані), не monitoring/.
"""

import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import psycopg2.extras

logger = logging.getLogger(__name__)

# 0 (2026-10-02, живий фідбек користувача — "сама логіка джоби
# неправильна": check_releases раніше чекав 30 хв ПІСЛЯ scheduled_at,
# перш ніж ЗАГАЛОМ почати пробувати, а бігав лише раз на 15 хв —
# комбінація давала затримку детекції до ~44 хв навіть коли scheduler
# живий. Тепер check_releases сам крутиться щохвилини
# (orchestration/schedule.py), тому буфер-затримка перед першою спробою
# більше не потрібна — пробуємо РІВНО в scheduled_at і далі щохвилини,
# доки джерело не опублікує дані (якщо публікація сама запізнилась —
# це не помилка, просто кілька порожніх спроб поспіль: адаптер
# повертає 0 нових записів, рядок лишається pending). docs/decisions.md
# 2026-10-02.
DEFAULT_BUFFER_MINUTES = 0


# Наскільки дата збереженого циклу може відрізнятись від `due_date`,
# і це все одно ТОЙ САМИЙ цикл.
#
# ЧОМУ НЕ НУЛЬ (виправлення 2026-10-13). Порівняння було точним
# (`existing.date() != due_date`) і працювало, доки всі календарні
# показники були US. Коли `official_calendars.py` додав Японію,
# з'явився дубль: японський CPI публікується о 08:30 JST, тобто
# 23:30 UTC ПОПЕРЕДНЬОГО дня. `due_date` — токійська дата (23
# жовтня), а `scheduled_at.date()` при читанні з БД — UTC-дата (22
# жовтня). Дати не збігались, тож КОЖЕН прогін заводив новий
# 'pending' рядок. Живий наслідок: два однакові записи japan_cpi за
# два прогони.
#
# Один день допуску безпечний: найчастіші наші релізи — тижневі
# (`initial_jobless_claims`, `mortgage_rate_30y`), тобто два РІЗНІ
# цикли не можуть опинитись у межах доби. Альтернатива — тягати
# часовий пояс показника в порівняння — складніша й ламкіша: час
# може прийти і з ForexFactory, і з офіційної сторінки, і з
# fallback, у кожного свій пояс.
SAME_CYCLE_TOLERANCE_DAYS = 1


def should_seed_new_cycle(existing_scheduled_at: Optional[datetime], due_date: date) -> bool:
    """Чиста логіка: чи треба заводити новий 'pending' рядок для цього
    due_date (з release_calendar.next_scheduled_release()).

    Порівнюємо за ДАТОЮ (не повним datetime) — уточнення точного часу
    з economic_calendar.py на пізнішому прогоні (той самий цикл, той
    самий due_date) не повинно плодити дублікат; ідентичність циклу —
    сама дата (авторитетне джерело), час — лише збагачення.

    З допуском `SAME_CYCLE_TOLERANCE_DAYS` (обґрунтування вище):
    дата в UTC і дата в локальному поясі релізу можуть відрізнятись
    на добу, і це не різні цикли."""
    if existing_scheduled_at is None:
        return True
    delta = abs((existing_scheduled_at.date() - due_date).days)
    return delta > SAME_CYCLE_TOLERANCE_DAYS


def is_past_buffer(scheduled_at: datetime, now: datetime, buffer_minutes: int = DEFAULT_BUFFER_MINUTES) -> bool:
    """Чиста логіка: чи вже настав запланований час публікації (+
    buffer_minutes, якщо викликач хоче навмисну затримку — за
    замовчуванням 0, див. коментар біля DEFAULT_BUFFER_MINUTES)."""
    return now >= scheduled_at + timedelta(minutes=buffer_minutes)


@contextmanager
def _cursor(conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield cur
    finally:
        cur.close()


def get_latest_log_entry(conn, source: str, metric_id: str) -> Optional[dict]:
    with _cursor(conn) as cur:
        cur.execute(
            """
            SELECT id, scheduled_at, detected_at, impact_level, expected_value, status
            FROM release_log
            WHERE source = %s AND metric_id = %s
            ORDER BY scheduled_at DESC
            LIMIT 1
            """,
            (source, metric_id),
        )
        return cur.fetchone()


def insert_pending(
    conn,
    source: str,
    metric_id: str,
    scheduled_at: datetime,
    impact_level: Optional[str],
    expected_value: Optional[str] = None,
) -> int:
    with _cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO release_log (source, metric_id, scheduled_at, impact_level, expected_value, status)
            VALUES (%s, %s, %s, %s, %s, 'pending')
            RETURNING id
            """,
            (source, metric_id, scheduled_at, impact_level, expected_value),
        )
        new_id = cur.fetchone()["id"]
    conn.commit()
    logger.info("release_log: заведено pending %s/%s за %s (id=%s)", source, metric_id, scheduled_at, new_id)
    return new_id


def update_enrichment(
    conn,
    log_id: int,
    scheduled_at: datetime,
    impact_level: Optional[str],
    expected_value: Optional[str],
) -> None:
    """Донасичує вже заведений 'pending' рядок точнішими даними, коли
    economic_calendar.py знаходить збіг ПІЗНІШЕ, ніж перший seed (live-
    факт 2026-09-26: ForexFactory-фід "thisweek" не "перекочується" на
    новий тиждень заздалегідь — при seed за кілька днів до фактичного
    релізу фід ще показує СТАРИЙ тиждень, тож перший прогін іде на
    fallback; наступний тижневий прогін, коли фід уже оновився, повинен
    мати змогу підмінити fallback на реальні дані, не просто мовчки
    пропустити через `should_seed_new_cycle() == False`)."""
    with _cursor(conn) as cur:
        cur.execute(
            """
            UPDATE release_log
            SET scheduled_at = %s, impact_level = %s, expected_value = %s
            WHERE id = %s
            """,
            (scheduled_at, impact_level, expected_value, log_id),
        )
    conn.commit()
    logger.info("release_log: донасичено id=%s — %s, impact=%s, forecast=%r", log_id, scheduled_at, impact_level, expected_value)


def get_pending_entries(conn) -> list[dict]:
    """Усі рядки 'pending' — по всіх джерелах (fred/ecb/boj/estat), не
    тільки одному: check_releases.py сам обирає адаптер за `source`
    кожного рядка (`monitoring/metric_sources.py`). Фільтрація "чи вже
    минув буфер" робиться в Python (`is_past_buffer()`), не тут, щоб
    логіка буфера жила в одному місці (той самий принцип, що чиста
    логіка окремо від SQL)."""
    with _cursor(conn) as cur:
        cur.execute(
            """
            SELECT id, source, metric_id, scheduled_at, impact_level, expected_value
            FROM release_log
            WHERE status = 'pending'
            ORDER BY scheduled_at
            """
        )
        return cur.fetchall()


def mark_detected(conn, log_id: int) -> None:
    with _cursor(conn) as cur:
        cur.execute(
            """
            UPDATE release_log SET status = 'detected', detected_at = %s
            WHERE id = %s
            """,
            (datetime.now(timezone.utc), log_id),
        )
    conn.commit()
    logger.info("release_log: id=%s позначено detected", log_id)


def get_detected_entries(conn) -> list[dict]:
    """Усі рядки 'detected' — вхід для analysis/expectations/compare_releases.py
    (порівняння факту з `expected_value`). Той самий принцип, що
    `get_pending_entries()`: monitoring лише детектує, перехід у
    'processed' — робота analysis/, коли вона фактично використала ці
    дані (release_log.py docstring)."""
    with _cursor(conn) as cur:
        cur.execute(
            """
            SELECT id, source, metric_id, scheduled_at, impact_level, expected_value
            FROM release_log
            WHERE status = 'detected'
            ORDER BY scheduled_at
            """
        )
        return cur.fetchall()


def mark_processed(conn, log_id: int) -> None:
    with _cursor(conn) as cur:
        cur.execute(
            """
            UPDATE release_log SET status = 'processed'
            WHERE id = %s
            """,
            (log_id,),
        )
    conn.commit()
    logger.info("release_log: id=%s позначено processed", log_id)
