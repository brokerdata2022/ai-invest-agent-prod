"""
Тонкий SQL-шар для calendar_outlook — той самий принцип, що
expectations/_db.py: SQL без інтерпретації значень, сама логіка (вибір
діапазону дат, промпт, парсинг відповіді LLM) лишається в run_outlook.py/
synthesize.py.
"""

from datetime import date


def fetch_upcoming(conn, start: date, end: date) -> list[dict]:
    """'pending'-рядки release_log у діапазоні [start, end] (включно) —
    саме ті, що вже заведені наперед refresh_calendar.py, але ще не
    випущені."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, source, metric_id, scheduled_at, impact_level, expected_value
            FROM release_log
            WHERE status = 'pending'
              AND scheduled_at::date BETWEEN %s AND %s
            ORDER BY scheduled_at
            """,
            (start, end),
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def has_outlook(conn, outlook_date: date, scope: str) -> bool:
    """Чи вже згенеровано огляд на цю дату/scope — ідемпотентність на
    ретраї orchestration/runner.py (не бити LLM вдруге за той самий
    день)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM calendar_outlook WHERE outlook_date = %s AND scope = %s",
            (outlook_date, scope),
        )
        return cur.fetchone() is not None


def save_outlook(
    conn,
    outlook_date: date,
    scope: str,
    release_log_ids: list[int],
    result,
    llm_call_id: int | None,
) -> int:
    """result — llm_common.SynthesisResult (або еквівалентний об'єкт із
    direction/confidence/summary/reasoning)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO calendar_outlook
                (outlook_date, scope, release_log_ids, direction, confidence,
                 summary, reasoning, llm_call_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (outlook_date, scope) DO NOTHING
            RETURNING id
            """,
            (
                outlook_date, scope, release_log_ids, result.direction,
                result.confidence, result.summary, result.reasoning, llm_call_id,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else None
