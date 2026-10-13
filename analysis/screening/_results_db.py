"""
Тонкий SQL-шар для результату composite_score.py — той самий принцип,
що analysis/news_analysis/_db.py: SQL без жодної інтерпретації значень.
Окремий від _batch_db.py (той — читання для tier_a/b/c, тут — запис
фінального результату скринінгу).
"""

from datetime import datetime, timezone


def save_screening_run(conn, results: list) -> int:
    """results — list[composite_score.CompositeResult]. Один run_at на
    весь виклик, щоб усі тикери одного прогону групувались однозначно
    (fetch_latest_tickers: MAX(run_at)). Порожній results — нічого не
    вставляє (docs/decisions.md: не затирати вчорашній валідний
    скринінг нульовим/збійним прогоном)."""
    if not results:
        return 0

    run_at = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO screening_results
                (ticker, score, revenue_growth, eps_growth, pe, avg_dollar_volume,
                 company_name, price_change_24h_pct, price_date, volume_change_24h_pct, run_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    r.ticker, r.score, r.revenue_growth, r.eps_growth, r.pe, r.avg_dollar_volume,
                    getattr(r, "company_name", None), getattr(r, "price_change_24h_pct", None),
                    getattr(r, "price_date", None), getattr(r, "volume_change_24h_pct", None), run_at,
                )
                for r in results
            ],
        )
    conn.commit()
    return len(results)


def fetch_latest_tickers(conn) -> list[str]:
    """Тикери останнього прогону (MAX(run_at)) — порожній список, якщо
    таблиця ще порожня (жодного прогону з збереженням ще не було)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ticker FROM screening_results
            WHERE run_at = (SELECT MAX(run_at) FROM screening_results)
            ORDER BY ticker
            """
        )
        return [row[0] for row in cur.fetchall()]
