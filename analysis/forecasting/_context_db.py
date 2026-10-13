"""
SQL для ринкового контексту прогнозу — тонкий шар, без інтерпретації
(той самий принцип, що решта `_db.py`): рахунки й форматування живуть
у `market_context.py` (чисті функції).
"""

from datetime import date
from typing import Optional


def fetch_series(conn, source: str, metric_id: str, limit: int = 2000) -> list[tuple[date, float]]:
    """Увесь ряд як [(дата, значення)], найстаріше перше.

    Беремо ПОВНУ історію один раз, а не "останні N на кожну точку":
    бектест рахує контекст станом на КОЖНУ перевірену точку
    (`market_context.value_as_of`), і робити для цього окремий запит на
    точку означало б 50 запитів на показник замість одного."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT observed_at, value
            FROM v_observations_latest_revision
            WHERE source = %s AND metric_id = %s
            ORDER BY observed_at
            LIMIT %s
            """,
            (source, metric_id, limit),
        )
        return [(row[0], float(row[1])) for row in cur.fetchall()]
