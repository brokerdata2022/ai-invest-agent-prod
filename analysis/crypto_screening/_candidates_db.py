"""
Тонкий SQL-шар для crypto_screening_candidates — той самий принцип, що
analysis/screening/_results_db.py: SQL без інтерпретації значень.

На відміну від screening_results (акції, append-only знімок ОДНОГО
прогону), тут МУТАБЕЛЬНИЙ стан: `upsert_candidate()` (денний скан,
run_screening.py) заводить рядок при виявленні пампу або лишає
активний як є; `update_candidate()` (погодинний моніторинг,
monitor_candidates.py) оновлює його — переводить статус або закриває,
коли памп вичерпався (докладніше docs/decisions.md, 2026-09-27).
"""

import psycopg2.extras


def upsert_candidate(
    conn,
    symbol: str,
    pump_pct: float,
    funding_rate,
    oi_usd,
    price=None,
) -> int:
    """Якщо для symbol уже є АКТИВНИЙ (не closed) рядок — нічого не
    змінює, повертає його id (денний скан не повинен затирати прогрес
    погодинного моніторингу). Інакше заводить новий 'candidate'.

    `price` — Binance mark_price на момент детекції (None, якщо символ
    відсутній на Binance) — перша точка відліку для погодинної
    %-зміни ціни (`monitor_candidates.py`, 2026-10-03). Без
    `quote_volume` (2026-10-03) — сплеск обсягу тепер рахується з 4г-
    Binance klines "на льоту" (`run_screening.py:
    compute_monitoring_indicators`), не зі збереженого 24h-знімка, тож
    `last_quote_volume` більше ніде не читався."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM crypto_screening_candidates WHERE symbol = %s AND status != 'closed'",
            (symbol,),
        )
        existing = cur.fetchone()
        if existing:
            return existing[0]

        cur.execute(
            """
            INSERT INTO crypto_screening_candidates
                (symbol, status, pump_pct_at_detection, last_pump_pct,
                 last_funding_rate, last_oi_usd, last_price, last_checked_at)
            VALUES (%s, 'candidate', %s, %s, %s, %s, %s, now())
            RETURNING id
            """,
            (symbol, pump_pct, pump_pct, funding_rate, oi_usd, price),
        )
        new_id = cur.fetchone()[0]
    conn.commit()
    return new_id


def fetch_active_candidates(conn) -> list[dict]:
    """Усі рядки зі статусом != 'closed' — вхід для погодинного
    моніторингу (monitor_candidates.py). `status` включено явно (не
    лише для фільтра WHERE) — caller передає його в update_candidate()
    як old_status, щоб відрізнити "статус не змінився" від "змінився"."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, symbol, status, pump_pct_at_detection, last_pump_pct,
                   last_funding_rate, last_oi_usd, last_price, detected_at
            FROM crypto_screening_candidates
            WHERE status != 'closed'
            ORDER BY detected_at
            """
        )
        return cur.fetchall()


def update_candidate(
    conn,
    candidate_id: int,
    old_status: str,
    status: str,
    pump_pct: float,
    funding_rate,
    oi_usd,
    reason: str,
    price=None,
) -> None:
    """Оновлює знімок ("минулого разу") для наступної погодинної
    дельти, і статус. `closed_at` проставляється лише при переході в
    'closed' (COALESCE — повторний перехід у closed не перезаписує
    оригінальний момент закриття). `price` — поточний Binance
    mark_price (None, якщо символ відсутній на Binance) — стає
    `last_price` для НАСТУПНОЇ погодинної дельти (2026-10-03).

    `notified_at` скидається на NULL, коли status РЕАЛЬНО змінився
    (напр. candidate→short) — 2026-10-02, той самий принцип, що вже
    застосований до news_synthesis/market_synthesis (docs/decisions.md,
    2026-09-29): без цього significant перехід лишився б непоміченим
    під уже проставленим notified_at (reporting/crypto_screening_notify.py
    більше не надіслав би його). Якщо status не змінився — notified_at
    лишається як є (не скидати на кожен погодинний тик, інакше дедуп
    узагалі не працює)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE crypto_screening_candidates
            SET status = %s,
                last_pump_pct = %s,
                last_funding_rate = %s,
                last_oi_usd = %s,
                last_price = %s,
                reason = %s,
                last_checked_at = now(),
                closed_at = CASE WHEN %s = 'closed' THEN COALESCE(closed_at, now()) ELSE closed_at END,
                notified_at = CASE WHEN %s != %s THEN NULL ELSE notified_at END
            WHERE id = %s
            """,
            (
                status, pump_pct, funding_rate, oi_usd, price, reason,
                status, status, old_status, candidate_id,
            ),
        )
    conn.commit()
