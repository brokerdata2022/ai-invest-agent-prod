"""
Тонкий SQL-шар вердиктів бектесту (`backtest_verdicts`) — той самий
принцип, що решта `_db.py` у проєкті: SQL без інтерпретації значень,
класифікація лишається в `method_selector.py` (чисті функції).

Таблиця append-only (db/schema.sql): три прогони того самого показника
різними моделями — три рядки. "Поточний вердикт" для методу — останній
рядок для КОНКРЕТНОЇ моделі, якою прогноз реально робиться, а не
"найкращий з усіх": інакше вибір методу спирався б на модель, яку ми
не запускаємо.
"""

from typing import Optional


def save_verdict(
    conn,
    metric_id: str,
    provider: str,
    model: str,
    effort: Optional[str],
    n_points: int,
    n_skipped: int,
    llm_mae: Optional[float],
    naive_mae: Optional[float],
    trend_mae: Optional[float],
    verdict: str,
    advantage_pct: Optional[float],
    prompt_variant: Optional[str] = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO backtest_verdicts
                (metric_id, provider, model, effort, n_points, n_skipped,
                 llm_mae, naive_mae, trend_mae, verdict, advantage_pct,
                 prompt_variant)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                metric_id, provider, model, effort, n_points, n_skipped,
                llm_mae, naive_mae, trend_mae, verdict, advantage_pct,
                prompt_variant,
            ),
        )
        verdict_id = cur.fetchone()[0]
    conn.commit()
    return verdict_id


def fetch_verdict(
    conn, metric_id: str, model: str, prompt_variant: Optional[str] = None
) -> Optional[dict]:
    """Останній вердикт для (показник, МОДЕЛЬ). None — не міряли.

    Фільтр саме за моделлю, не лише за провайдером: на Anthropic дві
    моделі з різною ціною й різним результатом, і вердикт Opus 5 нічого
    не каже про Sonnet 5 (живо: `unemployment_rate` — нічия на Sonnet,
    −60% на Opus).

    `prompt_variant` — з тієї ж причини (2026-10-11, живий дефект):
    `mortgage_rate_30y` на промпті `levels` дає −3.8%, а на
    `levels+yields` — +16.0%. Без фільтра брався б просто НАЙНОВІШИЙ
    рядок, тож експериментальний прогін іншим промптом тихо
    перемикав би бойовий метод. None = будь-який варіант (для
    сумісності й для оглядових запитів)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT verdict, advantage_pct, model, n_points, prompt_variant, run_at
            FROM backtest_verdicts
            WHERE metric_id = %s AND model = %s
              AND (%s IS NULL OR prompt_variant = %s)
            ORDER BY run_at DESC
            LIMIT 1
            """,
            (metric_id, model, prompt_variant, prompt_variant),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cur.description]
        return dict(zip(columns, row))


def fetch_all_latest(conn) -> list[dict]:
    """Останній вердикт кожної пари (показник, модель) — для огляду
    "що зараз чим прогнозується". Читається звітом і людиною, не
    бойовим прогнозом."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (metric_id, model)
                   metric_id, model, provider, verdict, advantage_pct,
                   llm_mae, naive_mae, n_points, prompt_variant, run_at
            FROM backtest_verdicts
            ORDER BY metric_id, model, run_at DESC
            """
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
