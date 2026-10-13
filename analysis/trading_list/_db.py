"""
Тонкий SQL-шар торгового списку — той самий принцип, що
`expectations/_db.py` і `calendar_outlook/_db.py`: SQL без інтерпретації
значень, уся логіка (скоринг, пороги, відбір) лишається в
`scoring.py`/`config.py`.

Читає з пʼяти різних місць, бо торговий список за визначенням зводить
разом універсум і каталізатори:
- універсум: `screening_results`, `crypto_screening_candidates`,
  `crypto_long_candidates`, `watchlist_assets`;
- каталізатори: `release_log`, `expectation_synthesis.asset_impacts`,
  `news_consolidated`, `news_synthesis`, `metric_forecasts`;
- ціни для тренду: `v_observations_latest_revision`.
"""

from decimal import Decimal
from typing import Optional


def _dicts(cur) -> list[dict]:
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


# ============================================================
# УНІВЕРСУМ
# ============================================================


def fetch_stock_universe(conn) -> list[dict]:
    """Акції останнього прогону скринінгу (`MAX(run_at)`) — уже
    відфільтровані за якістю й оцінкою Tier A/B/C. Їх близько 215, і
    саме тому поверх них потрібен фільтр каталізатора."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ticker, company_name, score, price_change_24h_pct
            FROM screening_results
            WHERE run_at = (SELECT MAX(run_at) FROM screening_results)
            ORDER BY score DESC
            """
        )
        return _dicts(cur)


def fetch_crypto_universe(conn) -> list[dict]:
    """Крипто-кандидати з ЖИВИМ сетапом. Два джерела, бо скринінг
    крипти ведеться двома окремими треками:
    - `crypto_screening_candidates` — SHORT/WATCH (мутабельний стан між
      прогонами, тому фільтр за `status`, не за `run_at`);
    - `crypto_long_candidates` — LONG (знімок прогону, як
      screening_results, тому `MAX(run_at)`).

    `closed` свідомо виключені: сетап уже відіграв."""
    rows: list[dict] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, status, last_pump_pct, pump_pct_at_detection,
                   last_funding_rate, last_oi_usd, reason
            FROM crypto_screening_candidates
            WHERE status IN ('candidate', 'short', 'watch')
            ORDER BY detected_at DESC
            """
        )
        rows.extend({**r, "setup": r["status"]} for r in _dicts(cur))

        cur.execute(
            """
            SELECT symbol, oi_change_pct, rsi_value, funding_rate
            FROM crypto_long_candidates
            WHERE run_at = (SELECT MAX(run_at) FROM crypto_long_candidates)
            ORDER BY symbol
            """
        )
        rows.extend({**r, "setup": "long"} for r in _dicts(cur))
    return rows


def fetch_watchlist_universe(conn) -> list[dict]:
    """Активи, обрані користувачем. `source`/`metric_id` потрібні, щоб
    дістати саме той ряд цін, який для цього активу реально
    збирається (джерело могло автоматично перемкнутись —
    `common/watchlist_db.py:choose_freshest_source`)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, label, source, metric_id, ticker
            FROM watchlist_assets
            WHERE enabled
            ORDER BY asset_id
            """
        )
        return _dicts(cur)


# ============================================================
# ЦІНИ ДЛЯ ТРЕНДУ
# ============================================================


def fetch_closes(conn, source: str, metric_id: str, limit: int) -> list[Decimal]:
    """Денні закриття в ХРОНОЛОГІЧНОМУ порядку (найстаріше перше) —
    саме так їх очікує `scoring.trend_score()`.

    Читає з `v_observations_latest_revision`, не з `raw_observations`:
    для кожної дати лишається тільки остання відома ревізія, інакше
    тренд рахувався б по "шуму" старих ревізій (той самий принцип, що
    `common/db.py:fetch_recent`)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT value
            FROM v_observations_latest_revision
            WHERE source = %s AND metric_id = %s
            ORDER BY observed_at DESC
            LIMIT %s
            """,
            (source, metric_id, limit),
        )
        values = [row[0] for row in cur.fetchall()]
    return list(reversed(values))


# ============================================================
# КАТАЛІЗАТОРИ
# ============================================================


def fetch_upcoming_releases(conn, days: int) -> list[dict]:
    """Заплановані релізи в найближчі `days` днів (`pending`). Напрямку
    тут немає й бути не може — подія відома, результат ні; саме тому
    `CatalystHit.direction` для них 'neutral'."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT metric_id, scheduled_at, impact_level
            FROM release_log
            WHERE status = 'pending'
              AND scheduled_at >= now()
              AND scheduled_at < now() + make_interval(days => %s)
            ORDER BY scheduled_at
            """,
            (days,),
        )
        return _dicts(cur)


def fetch_recent_impacts(conn, days: int) -> list[dict]:
    """Міжактивні впливи ВИЙШЛИХ релізів за останні `days` днів —
    `expectation_synthesis.asset_impacts` (JSONB-масив
    category/assets/direction/explanation).

    Саме `category` використовується для звʼязування з активом, НЕ
    `assets` (вільний текст LLM) — обґрунтування в
    `trading_list/categories.py`."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ec.metric_id, es.asset_impacts, es.direction AS release_direction,
                   es.summary, ec.observed_at
            FROM expectation_synthesis es
            JOIN expectation_comparisons ec ON ec.id = es.comparison_id
            WHERE es.asset_impacts IS NOT NULL
              AND es.created_at >= now() - make_interval(days => %s)
            ORDER BY es.created_at DESC
            """,
            (days,),
        )
        return _dicts(cur)


def fetch_news_catalysts(conn, hours: int, min_source_count: int) -> list[dict]:
    """Новинні кластери з достатньою МЕХАНІЧНОЮ важливістю
    (`source_count` — скільки незалежних джерел написали те саме).

    Чому source_count, а не LLM-confidence: рішення 2026-09-28
    (docs/decisions.md) — механічна кластеризація замінила
    LLM-confidence як головний критерій важливості.

    `asset_id` тут може бути і watchlist-активом, і тикером акції
    (`consolidate.py` тегує тикери зі скринінгу) — саме тому цей
    каталізатор РІЗНИТЬ акції між собою, на відміну від макро."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, summary, direction, confidence, source_count
            FROM news_consolidated
            WHERE asset_id IS NOT NULL
              AND source_count >= %s
              AND created_at >= now() - make_interval(hours => %s)
            ORDER BY source_count DESC, created_at DESC
            """,
            (min_source_count, hours),
        )
        return _dicts(cur)


def fetch_synthesis_catalysts(conn, hours: int, min_confidence: Decimal) -> list[dict]:
    """LLM-синтез "ціна↔новини" по активу (`news_synthesis`) —
    причинний звʼязок між рухом ціни й новинним фоном, із напрямком."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, summary, direction, confidence, price_pct_change
            FROM news_synthesis
            WHERE confidence >= %s
              AND created_at >= now() - make_interval(hours => %s)
            ORDER BY created_at DESC
            """,
            (min_confidence, hours),
        )
        return _dicts(cur)


def fetch_trusted_forecasts(conn, metric_ids: frozenset[str]) -> list[dict]:
    """НАШ власний прогноз — лише по показниках із
    `config.FORECAST_TRUSTED_METRICS` (ті, де backtest показав перевагу
    над naive).

    Порожній `metric_ids` → порожній результат без запиту: `= ANY('{}')`
    однаково нічого не повернув би, але так явніше й без зайвого
    звернення до БД."""
    if not metric_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (metric_id)
                   metric_id, forecast_value, direction, confidence, summary,
                   based_on_observed_at
            FROM metric_forecasts
            WHERE method = 'llm'
              AND direction IS NOT NULL
              AND metric_id = ANY(%s)
            ORDER BY metric_id, created_at DESC
            """,
            (sorted(metric_ids),),
        )
        return _dicts(cur)


# ============================================================
# ЗБЕРЕЖЕННЯ
# ============================================================


def save_trading_list(conn, rows: list[dict], horizon: str) -> int:
    """Append-only знімок прогону: усі рядки отримують ОДИН `run_at`
    (default now() у межах однієї транзакції), "поточний список" =
    `MAX(run_at)`.

    ПОРОЖНІЙ прогін нічого не вставляє — свідомо, той самий принцип,
    що `screening_results` (db/schema.sql): тихий день чи збійний
    прогін не має затирати попередній валідний список. Повертає
    кількість вставлених рядків."""
    if not rows:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO trading_list
                (asset_id, label, kind, horizon, direction, score,
                 catalyst_score, trend_score, quality_score,
                 catalyst_summary, reasons, source)
            VALUES (%(asset_id)s, %(label)s, %(kind)s, %(horizon)s, %(direction)s, %(score)s,
                    %(catalyst_score)s, %(trend_score)s, %(quality_score)s,
                    %(catalyst_summary)s, %(reasons)s, %(source)s)
            """,
            [{**row, "horizon": horizon} for row in rows],
        )
        inserted = cur.rowcount
    conn.commit()
    return inserted


def fetch_current_list(conn, horizon: str) -> list[dict]:
    """Поточний список (останній прогін цього горизонту) — для
    `reporting/trading_list_notify.py`. `notified_at` віддається, щоб
    notify-скрипт сам вирішував про дедуп (reporting/_common.py)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, asset_id, kind, direction, score, catalyst_score,
                   trend_score, quality_score, catalyst_summary, source,
                   run_at, notified_at
            FROM trading_list
            WHERE horizon = %s
              AND run_at = (SELECT MAX(run_at) FROM trading_list WHERE horizon = %s)
            ORDER BY score DESC
            """,
            (horizon, horizon),
        )
        return _dicts(cur)
