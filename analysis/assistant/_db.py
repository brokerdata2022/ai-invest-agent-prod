"""
SQL добору контексту для відповіді на питання.

**Чому структурований добір, а не векторний пошук.** Корпус новин живе
48 годин і вже класифікований по активах (`news_consolidated.asset_id`),
а решта даних — це таблиці з ключем «актив + дата». Пошук по активу й
даті тут і точніший, і дешевший за embeddings; векторне сховище було б
інфраструктурою заради інфраструктури (та сама логіка, що відкинула
Celery/Airflow — docs/decisions.md 2026-09-27).

**`asset_id` змішаний за регістром** (живо: `AAPL`, `ETH`, `xagusd`) —
тикери приходять у верхньому, watchlist-активи в нижньому. Тому всі
порівняння регістронезалежні, інакше питання про ефір не знайшло б
новин, записаних як `ETH`.
"""

from typing import Optional


def _dicts(cur) -> list[dict]:
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


# ============================================================
# Ліміти (rate limit) — рахуються по журналу LLM-викликів
# ============================================================


def assistant_calls_today(conn, purpose: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM llm_call_log
            WHERE purpose = %s AND created_at >= date_trunc('day', now())
            """,
            (purpose,),
        )
        return cur.fetchone()[0]


def seconds_since_last_call(conn, purpose: str) -> Optional[float]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT extract(epoch FROM now() - max(created_at))
            FROM llm_call_log WHERE purpose = %s
            """,
            (purpose,),
        )
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None


# ============================================================
# Що ми взагалі ведемо
# ============================================================


def known_tickers(conn) -> set:
    """Тикери, по яких у нас є котирування. З БД, не з конфіга:
    універсум змінюється зі складом S&P, і хардкод застарівав би тихо
    (той самий підхід, що в `run_collect_earnings.py`)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT upper(replace(metric_id, '_close', ''))
            FROM raw_observations
            WHERE source = 'twelvedata' AND metric_id LIKE '%%_close'
            """
        )
        return {row[0] for row in cur.fetchall()}


# ============================================================
# Загальний контекст (завжди)
# ============================================================


def upcoming_releases(conn, limit: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT metric_id, scheduled_at, impact_level, expected_value
            FROM release_log
            WHERE status = 'pending' AND scheduled_at >= now()
            ORDER BY scheduled_at
            LIMIT %s
            """,
            (limit,),
        )
        return _dicts(cur)


def latest_market_synthesis(conn) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT session, direction, confidence, summary, created_at
            FROM market_synthesis ORDER BY created_at DESC LIMIT 1
            """
        )
        rows = _dicts(cur)
        return rows[0] if rows else None


def latest_calendar_outlook(conn) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT outlook_date, direction, summary
            FROM calendar_outlook ORDER BY created_at DESC LIMIT 1
            """
        )
        rows = _dicts(cur)
        return rows[0] if rows else None


def trading_list_snapshot(conn, limit: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, kind, direction, score, catalyst_summary
            FROM trading_list
            WHERE run_at = (SELECT max(run_at) FROM trading_list)
            ORDER BY score DESC
            LIMIT %s
            """,
            (limit,),
        )
        return _dicts(cur)


def outcome_summary(conn, horizon_days: int) -> list[dict]:
    """Зведена якість рішень — щоб на питання «а ти часто вгадуєш»
    відповідали зміряні числа, а не загальні слова."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT decision_kind,
                   count(*) FILTER (WHERE verdict = 'correct')   AS correct,
                   count(*) FILTER (WHERE verdict = 'incorrect') AS incorrect
            FROM decision_outcomes
            WHERE horizon_days = %s
            GROUP BY decision_kind ORDER BY decision_kind
            """,
            (horizon_days,),
        )
        return _dicts(cur)


# ============================================================
# Контекст по КОНКРЕТНОМУ активу
# ============================================================


def watchlist_series(conn, asset_id: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, label, source, metric_id
            FROM watchlist_assets WHERE asset_id = %s AND enabled
            """,
            (asset_id,),
        )
        rows = _dicts(cur)
        return rows[0] if rows else None


def price_points(conn, source: str, metric_id: str, days: int) -> list[dict]:
    """Денні точки за вікно, найновіше перше — з них проводка рахує
    зміну. Межа дати літеральна (`current_date - N`) не потрібна: тут
    один ряд, а не 450 запитів на прогін."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT observed_at, value
            FROM v_observations_latest_revision
            WHERE source = %s AND metric_id = %s
              AND observed_at >= current_date - %s
            ORDER BY observed_at DESC
            """,
            (source, metric_id, days),
        )
        return _dicts(cur)


def ticker_quote(conn, ticker: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT observed_at, value
            FROM v_observations_latest_revision
            WHERE source = 'twelvedata' AND metric_id = %s
            ORDER BY observed_at DESC LIMIT 1
            """,
            (f"{ticker.lower()}_close",),
        )
        rows = _dicts(cur)
        return rows[0] if rows else None


def ticker_screening(conn, ticker: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ticker, company_name, score, price_change_24h_pct, run_at
            FROM screening_results
            WHERE ticker = %s AND run_at = (SELECT max(run_at) FROM screening_results)
            """,
            (ticker,),
        )
        rows = _dicts(cur)
        return rows[0] if rows else None


def ticker_earnings(conn, ticker: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT report_date, report_time, eps_forecast
            FROM earnings_calendar
            WHERE ticker = %s AND report_date >= current_date
            ORDER BY report_date LIMIT 1
            """,
            (ticker,),
        )
        rows = _dicts(cur)
        return rows[0] if rows else None


def asset_in_trading_list(conn, key: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, direction, score, catalyst_score, trend_score,
                   quality_score, catalyst_summary, run_at
            FROM trading_list
            WHERE lower(asset_id) = lower(%s)
              AND run_at = (SELECT max(run_at) FROM trading_list)
            """,
            (key,),
        )
        rows = _dicts(cur)
        return rows[0] if rows else None


def asset_news(conn, key: str, limit: int) -> list[dict]:
    """Консолідовані новини по активу. Регістронезалежно: той самий
    актив зустрічається і як `ETH`, і як `eth`."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT summary, direction, confidence, source_count, created_at
            FROM news_consolidated
            WHERE lower(asset_id) = lower(%s)
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (key, limit),
        )
        return _dicts(cur)


def asset_synthesis(conn, key: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT summary, direction, confidence, price_pct_change, created_at
            FROM news_synthesis
            WHERE lower(asset_id) = lower(%s)
            ORDER BY created_at DESC LIMIT 1
            """,
            (key,),
        )
        rows = _dicts(cur)
        return rows[0] if rows else None


def asset_outcomes(conn, key: str, limit: int) -> list[dict]:
    """Як закінчились МИНУЛІ рішення по цьому активу — те, чого в
    жодному звіті не видно, а на питання «як ти справлявся з золотом»
    відповідає прямо."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT decision_kind, direction, decided_at, realized_pct, verdict
            FROM decision_outcomes
            WHERE lower(asset_key) = lower(%s)
            ORDER BY decided_at DESC
            LIMIT %s
            """,
            (key, limit),
        )
        return _dicts(cur)
