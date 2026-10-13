"""
Тонкий SQL-шар для crypto_long_candidates — той самий принцип, що
screening/_results_db.py (акції): append-only ЗНІМОК одного прогону,
НЕ мутабельний стан, як crypto_screening_candidates (SHORT/WATCH) —
LONG-сигнал це "сьогодні тренд підтверджений" для вже Tier A-
допущеного символу, не памп, що вичерпується з часом, тож немає
статусу, який змінюється, і немає потреби в погодинному моніторингу.
"""

from datetime import datetime, timezone


def save_long_run(conn, signals: list) -> int:
    """signals — list[long_screen.LongSignal] (уже якісні, qualifies=True,
    з проставленими run_screening.py oi_change_pct/rsi_value/funding_rate).
    Один run_at на весь виклик, як save_screening_run() (акції) —
    fetch_latest: MAX(run_at). Порожній signals — нічого не вставляє
    (той самий принцип: не затирати вчорашній валідний прогін нульовим)."""
    if not signals:
        return 0

    run_at = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO crypto_long_candidates
                (symbol, oi_change_pct, rsi_value, funding_rate, run_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            [
                (s.symbol, s.oi_change_pct, s.rsi_value, s.funding_rate, run_at)
                for s in signals
            ],
        )
    conn.commit()
    return len(signals)
