"""
Тонкий SQL-шар для metric_forecasts — той самий принцип, що
expectations/_db.py: SQL без інтерпретації значень, уся логіка
(промпт/розбір відповіді LLM/перевірка правдоподібності) лишається в
llm_forecast.py, базові моделі — у trend.py.
"""

from typing import Optional


def save_forecast(
    conn,
    source: str,
    metric_id: str,
    method: str,
    based_on_observed_at,
    periods_ahead: int,
    forecast_value: float,
    direction: Optional[str] = None,
    confidence: Optional[float] = None,
    summary: Optional[str] = None,
    reasoning: Optional[str] = None,
    watch_on_release: Optional[str] = None,
    llm_call_id: Optional[int] = None,
) -> int:
    """UPSERT — один рядок на (source, metric_id, method,
    based_on_observed_at): повторний прогін на тих самих вхідних даних
    (той самий останній спостережений період) оновлює рядок, не
    дублює (db/schema.sql:idx_metric_forecasts_unique).

    Поля direction..llm_call_id заповнює лише LLM-прогноз
    (method='llm', llm_forecast.py) — для `linear_trend` їх не існує й
    не має існувати, тому вони опціональні (db/schema.sql: усі
    NULLABLE).

    `notified_at` при UPDATE СКИДАЄТЬСЯ в NULL — прогноз на тих самих
    вхідних даних, але з новим числом/обґрунтуванням, це новий
    висновок, його треба надіслати ще раз (інакше виправлений прогноз
    тихо не дійшов би до Telegram, бо reporting/forecast_notify.py
    фільтрує за notified_at IS NULL)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO metric_forecasts
                (source, metric_id, method, based_on_observed_at, periods_ahead,
                 forecast_value, direction, confidence, summary, reasoning,
                 watch_on_release, llm_call_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, metric_id, method, based_on_observed_at) DO UPDATE SET
                periods_ahead = EXCLUDED.periods_ahead,
                forecast_value = EXCLUDED.forecast_value,
                direction = EXCLUDED.direction,
                confidence = EXCLUDED.confidence,
                summary = EXCLUDED.summary,
                reasoning = EXCLUDED.reasoning,
                watch_on_release = EXCLUDED.watch_on_release,
                llm_call_id = EXCLUDED.llm_call_id,
                notified_at = NULL,
                created_at = now()
            RETURNING id
            """,
            (
                source, metric_id, method, based_on_observed_at, periods_ahead,
                forecast_value, direction, confidence, summary, reasoning,
                watch_on_release, llm_call_id,
            ),
        )
        forecast_id = cur.fetchone()[0]
    conn.commit()
    return forecast_id
