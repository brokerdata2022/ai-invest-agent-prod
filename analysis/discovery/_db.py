"""
Тонкий SQL-шар пошуку новачків — той самий принцип, що
`trading_list/_db.py`: SQL без інтерпретації значень, уся логіка
(пороги, скоринг, відбір) у `signals.py`/`config.py`.

Читає з трьох місць:
- **що ми ВЖЕ ведемо** (`watchlist_assets`, `crypto_screening_candidates`,
  `crypto_long_candidates`, останній `trading_list`) — щоб не подавати
  як "новачка" те, про що користувач і так отримує повідомлення;
- **ціни акцій, уже зібрані в БД** (`v_observations_latest_revision`,
  source='twelvedata') — детектор акцій не робить НІ ОДНОГО нового
  запиту до API (free tier Twelve Data майже вичерпується самим
  збором S&P 500);
- `candidate_assets` — запис результату.
"""

import json
from decimal import Decimal
from typing import Optional


def _dicts(cur) -> list[dict]:
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


# ============================================================
# ЩО ВЖЕ ВЕДЕМО (виключення з "новачків")
# ============================================================


def fetch_tracked_identifiers(conn) -> set[str]:
    """Усе, що вже в наших списках — у ВЕРХНЬОМУ регістрі, одним
    множинним набором: asset_id/тикери watchlist, символи активних
    крипто-кандидатів, активи останнього прогону торгового списку.

    "Новачок" означає "ще не в наших списках" — інакше той самий BTC
    приходив би і крипто-скринінгом, і торговим списком, і тут. Акції
    зі `screening_results` СВІДОМО не виключаються: там 215 рядків
    фундаментальної якості без жодної ознаки руху, і різкий рух такої
    акції — це саме новина, якої інші джерела не покажуть."""
    tracked: set[str] = set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_id, ticker, metric_id FROM watchlist_assets WHERE enabled"
        )
        for asset_id, ticker, metric_id in cur.fetchall():
            tracked.add(str(asset_id).upper())
            if ticker:
                tracked.add(str(ticker).upper().replace("/", ""))
            if metric_id:
                tracked.add(str(metric_id).upper().removesuffix("_CLOSE"))

        cur.execute(
            """
            SELECT symbol FROM crypto_screening_candidates
            WHERE status IN ('candidate', 'short', 'watch')
            """
        )
        tracked.update(str(row[0]).upper() for row in cur.fetchall())

        cur.execute(
            """
            SELECT symbol FROM crypto_long_candidates
            WHERE run_at = (SELECT MAX(run_at) FROM crypto_long_candidates)
            """
        )
        tracked.update(str(row[0]).upper() for row in cur.fetchall())

        cur.execute(
            """
            SELECT asset_id FROM trading_list
            WHERE run_at = (SELECT MAX(run_at) FROM trading_list)
            """
        )
        tracked.update(str(row[0]).upper() for row in cur.fetchall())
    return tracked


# ============================================================
# ЦІНИ АКЦІЙ, УЖЕ ЗІБРАНІ В БД
# ============================================================


def fetch_stock_series(conn, lookback_days: int) -> dict[str, dict]:
    """{TICKER: {"closes": [Decimal...], "volumes": [Decimal...]}} у
    ХРОНОЛОГІЧНОМУ порядку (найстаріше перше).

    ОДИН запит на весь універсум, не по тикеру (той самий урок N+1, що
    `screening/tier_a.py`, docs/decisions.md 2026-09-25): ~215 тикерів
    × 2 поля — це один SELECT на кілька десятків тисяч рядків, проти
    430 окремих.

    Читає з `v_observations_latest_revision`, не з `raw_observations` —
    інакше рух рахувався б по старих ревізіях (той самий принцип, що
    `trading_list/_db.py:fetch_closes`)."""
    series: dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT metric_id, value
            FROM v_observations_latest_revision
            WHERE source = 'twelvedata'
              AND (metric_id LIKE %s OR metric_id LIKE %s)
              AND observed_at >= (CURRENT_DATE - %s::int)
            ORDER BY metric_id, observed_at
            """,
            ("%\\_close", "%\\_volume", lookback_days),
        )
        for metric_id, value in cur.fetchall():
            if metric_id.endswith("_close"):
                ticker, key = metric_id[: -len("_close")], "closes"
            elif metric_id.endswith("_volume"):
                ticker, key = metric_id[: -len("_volume")], "volumes"
            else:
                continue
            bucket = series.setdefault(
                ticker.upper(), {"closes": [], "volumes": []}
            )
            bucket[key].append(value)
    return series


def fetch_crypto_volume_history(conn, days: int) -> dict[str, list[Decimal]]:
    """{SYMBOL: [денні $-обсяги...]} у ХРОНОЛОГІЧНОМУ порядку.

    Джерело — `bybit_futures` (metric_id `{symbol}_quote_volume`,
    data-ingestion/crypto/bybit_futures_adapter.py): єдина з 3 бірж,
    що віддає обсяг у bulk-запиті, тому єдина, чия історія в нас
    накопичується (`crypto_derivatives_collect`). Символ, якого на
    Bybit немає, просто не отримає обсягового сигналу — детектор
    трактує це як "невідомо", не як "спайку немає"
    (crypto_movers.py)."""
    history: dict[str, list[Decimal]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT metric_id, value
            FROM v_observations_latest_revision
            WHERE source = 'bybit_futures'
              AND metric_id LIKE %s
              AND observed_at >= (CURRENT_DATE - %s::int)
            ORDER BY metric_id, observed_at
            """,
            ("%\\_quote\\_volume", days),
        )
        for metric_id, value in cur.fetchall():
            symbol = metric_id[: -len("_quote_volume")].upper()
            history.setdefault(symbol, []).append(value)
    return history


def fetch_company_names(conn, tickers: list[str]) -> dict[str, str]:
    """Назви компаній з останнього прогону скринінгу — щоб у
    повідомленні був "NVDA — NVIDIA Corp", а не голий тикер. Тикера
    поза скринінгом тут просто не буде (детектор підставить сам
    тикер)."""
    if not tickers:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (ticker) ticker, company_name
            FROM screening_results
            WHERE ticker = ANY(%s)
            ORDER BY ticker, run_at DESC
            """,
            (tickers,),
        )
        return {row[0].upper(): row[1] for row in cur.fetchall()}


# ============================================================
# ЗАПИС РЕЗУЛЬТАТУ
# ============================================================


def save_candidate(conn, candidate) -> int:
    """UPSERT — один рядок на (ticker, день)
    (db/schema.sql:idx_candidate_assets_ticker_per_day).

    `notified_at = NULL` у DO UPDATE — той самий фікс і причина, що
    `news_analysis/_db.py:save_synthesis` (2026-09-29): без цього
    повторний прогін того самого дня з НОВИМИ числами мовчки не
    доходив би в Telegram."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO candidate_assets
                (ticker, company_name, reasoning, source_refs, llm_call_id,
                 asset_class, direction, detector, score, signals)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ticker, ((discovered_at AT TIME ZONE 'UTC')::date)) DO UPDATE SET
                company_name = EXCLUDED.company_name,
                reasoning = EXCLUDED.reasoning,
                source_refs = EXCLUDED.source_refs,
                llm_call_id = EXCLUDED.llm_call_id,
                asset_class = EXCLUDED.asset_class,
                direction = EXCLUDED.direction,
                detector = EXCLUDED.detector,
                score = EXCLUDED.score,
                signals = EXCLUDED.signals,
                discovered_at = now(),
                notified_at = NULL
            RETURNING id
            """,
            (
                candidate.asset_id,
                candidate.label,
                candidate.reason,
                json.dumps(candidate.source_refs),
                candidate.llm_call_id,
                candidate.asset_class,
                candidate.direction,
                candidate.detector,
                candidate.score,
                json.dumps(candidate.signals, default=str),
            ),
        )
        candidate_id = cur.fetchone()[0]
    conn.commit()
    return candidate_id
