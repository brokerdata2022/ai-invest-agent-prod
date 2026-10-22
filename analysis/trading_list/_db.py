"""
Тонкий SQL-шар торгового списку — той самий принцип, що
`expectations/_db.py` і `calendar_outlook/_db.py`: SQL без інтерпретації
значень, уся логіка (скоринг, пороги, відбір) лишається в
`scoring.py`/`config.py`.

Читає з пʼяти різних місць, бо торговий список за визначенням зводить
разом універсум і каталізатори:
- універсум: `screening_results`, `crypto_screening_candidates`,
  `crypto_long_candidates`, `watchlist_assets`, `candidate_assets`
  (новачки, з 2026-10-22);
- каталізатори: `release_log`, `expectation_synthesis.asset_impacts`,
  `news_consolidated`, `news_synthesis`, `metric_forecasts`;
- ціни для тренду: `v_observations_latest_revision`.
"""

import json
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
                   last_funding_rate, last_oi_usd, reason, last_price,
                   detected_at, last_checked_at
            FROM crypto_screening_candidates
            WHERE status IN ('candidate', 'short', 'watch')
            ORDER BY detected_at DESC
            """
        )
        rows.extend({**r, "setup": r["status"]} for r in _dicts(cur))

        cur.execute(
            """
            SELECT symbol, oi_change_pct, rsi_value, funding_rate, run_at AS detected_at
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
            SELECT asset_id, label, source, metric_id, ticker,
                   asset_class, commodity_group, company_name
            FROM watchlist_assets
            WHERE enabled
            ORDER BY asset_id
            """
        )
        return _dicts(cur)


def fetch_upcoming_earnings(conn, days: int) -> list[dict]:
    """Звіти компаній, що виходять у найближчі `days` днів
    (`earnings_calendar`, джерело — Nasdaq).

    Чому це тут, а не серед релізів: макрореліз стосується ВСЬОГО ринку
    й не різнить активи (тому приглушується `MACRO_DAMPING`), а звіт
    компанії — подія САМОГО тикера, як і новина про нього. Додано
    2026-10-15: доти акція мала прицільну подію лише за наявності
    новини (зміряно 13.10 — 9 кандидатів із 29)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ticker, report_date, report_time, eps_forecast
            FROM earnings_calendar
            -- Від УЧОРА: реакція на звіт після закриття — сьогоднішня
            -- подія для трейдера (2026-10-22, пункт 4 плану сесії).
            WHERE report_date >= current_date - 1
              AND report_date <= current_date + %s
            ORDER BY report_date, ticker
            """,
            (days,),
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
                   es.summary, ec.observed_at, es.created_at
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
            SELECT asset_id, summary, direction, confidence, source_count, created_at
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
            SELECT asset_id, summary, direction, confidence, price_pct_change, created_at
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


def fetch_discovery_candidates(conn, hours: int) -> list[dict]:
    """Новачки (`candidate_assets`, `analysis/discovery/`) за останні
    `hours` — ще один вхід списку для торгів (2026-10-22, пункт 4 плану
    сесії: "з УСІХ наших списків"). Доти новачки жили окремим
    повідомленням і в список торгів не потрапляли ніколи."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ticker, company_name, asset_class, direction, detector,
                   score, reasoning, discovered_at
            FROM candidate_assets
            WHERE discovered_at >= now() - make_interval(hours => %s)
            ORDER BY score DESC NULLS LAST, discovered_at DESC
            """,
            (hours,),
        )
        return _dicts(cur)


def save_trading_list(conn, rows: list[dict], horizon: str, run_summary: Optional[dict] = None) -> int:
    """Append-only знімок прогону: усі рядки отримують ОДИН `run_at`
    (default now() у межах однієї транзакції), "поточний список" =
    `MAX(run_at)`.

    ПОРОЖНІЙ прогін нічого не вставляє — свідомо, той самий принцип,
    що `screening_results` (db/schema.sql): тихий день чи збійний
    прогін не має затирати попередній валідний список. Повертає
    кількість вставлених рядків."""
    # Прогін записується ЗАВЖДИ, і порожній теж (2026-10-22): з
    # фільтром "каталізатор сьогодні" порожній список — законна
    # відповідь "сьогодні торгувати нічим", а не збій. Без цього рядка
    # notify показував би ВЧОРАШНІЙ список як сьогоднішній.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trading_list_runs (horizon, evaluated, passed, rejected)
            VALUES (%s, %s, %s, %s)
            RETURNING run_at
            """,
            (
                horizon,
                (run_summary or {}).get("evaluated", len(rows)),
                len(rows),
                json.dumps((run_summary or {}).get("rejected", {}), ensure_ascii=False),
            ),
        )
        (run_at,) = cur.fetchone()
        if rows:
            cur.executemany(
                """
                INSERT INTO trading_list
                    (asset_id, label, kind, horizon, direction, score,
                     catalyst_score, trend_score, quality_score,
                     catalyst_summary, reasons, source, levels, why, run_at)
                VALUES (%(asset_id)s, %(label)s, %(kind)s, %(horizon)s, %(direction)s, %(score)s,
                        %(catalyst_score)s, %(trend_score)s, %(quality_score)s,
                        %(catalyst_summary)s, %(reasons)s, %(source)s, %(levels)s, %(why)s, %(run_at)s)
                """,
                [{"levels": None, "why": None, **row, "horizon": horizon, "run_at": run_at} for row in rows],
            )
    conn.commit()
    return len(rows)


def fetch_current_list(conn, horizon: str) -> list[dict]:
    """Поточний список (останній прогін цього горизонту) — для
    `reporting/trading_list_notify.py`. `notified_at` віддається, щоб
    notify-скрипт сам вирішував про дедуп (reporting/_common.py)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, asset_id, kind, direction, score, catalyst_score,
                   trend_score, quality_score, catalyst_summary, source,
                   levels, why, run_at, notified_at
            FROM trading_list
            WHERE horizon = %s
              AND run_at = (SELECT MAX(run_at) FROM trading_list WHERE horizon = %s)
            ORDER BY score DESC
            """,
            (horizon, horizon),
        )
        return _dicts(cur)


def fetch_latest_run(conn, horizon: str) -> Optional[dict]:
    """Останній прогін списку (`trading_list_runs`) — зокрема ПОРОЖНІЙ:
    тоді звіт прямо каже "сьогодні торгувати нічим" і чому (скільки
    активів відсіяно за якою причиною)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_at, evaluated, passed, rejected
            FROM trading_list_runs
            WHERE horizon = %s
            ORDER BY run_at DESC
            LIMIT 1
            """,
            (horizon,),
        )
        rows = _dicts(cur)
    return rows[0] if rows else None
