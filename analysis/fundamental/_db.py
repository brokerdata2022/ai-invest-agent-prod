"""
Тонкий SQL-шар фундаментального аналізу — той самий принцип, що
`trading_list/_db.py` і `expectations/_db.py`: SQL без інтерпретації,
промпт і розбір відповіді лишаються в `fundamental_llm.py`.
"""

import json
from typing import Optional


def fetch_top_stocks(conn, limit: int) -> list[dict]:
    """Топ акцій останнього прогону скринінгу за composite-скором.

    Обмеження `limit` — головний контроль вартості: скринінг пропускає
    ~215 тикерів, а дані SEC EDGAR змінюються раз на КВАРТАЛ, тож
    щоденні 215 LLM-викликів були б витратою заради незмінних чисел
    (`config.TOP_N_STOCKS`)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ticker, company_name, score, pe, revenue_growth, eps_growth
            FROM screening_results
            WHERE run_at = (SELECT MAX(run_at) FROM screening_results)
            ORDER BY score DESC
            LIMIT %s
            """,
            (limit,),
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def fetch_watchlist_assets(conn) -> list[dict]:
    """Активи СПИСКУ ОБРАНИХ (рішення користувача 2026-10-04:
    "розгорнутий аналіз по активах із списку вибраних").

    `source`/`metric_id` потрібні, щоб узяти саме той ряд ціни, який
    для цього активу реально збирається — джерело могло автоматично
    перемкнутись (`common/watchlist_db.py:choose_freshest_source`)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, label, source, metric_id, ticker,
                   asset_class, commodity_group, company_name, cik
            FROM watchlist_assets
            WHERE enabled
            ORDER BY asset_id
            """
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def fetch_fundamental_series(conn, ticker: str, suffix: str, quarters: int) -> list[dict]:
    """Квартальний ряд одного показника в ХРОНОЛОГІЧНОМУ порядку.

    Читає `v_observations_latest_revision`, не `raw_observations`: SEC
    EDGAR перевидає ті самі періоди (ревізії звітності), і без цього в
    ряд потрапляли б і старі, уже виправлені значення."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT observed_at, value
            FROM v_observations_latest_revision
            WHERE source = 'sec_edgar' AND metric_id = %s
            ORDER BY observed_at DESC
            LIMIT %s
            """,
            (f"{ticker.lower()}_{suffix}", quarters),
        )
        rows = [{"observed_at": str(r[0]), "value": r[1]} for r in cur.fetchall()]
    return list(reversed(rows))


def save_fundamental_analysis(
    conn,
    asset_id: str,
    result,
    inputs: dict,
    llm_call_id: Optional[int],
    ticker: Optional[str] = None,
    company_name: Optional[str] = None,
    asset_kind: str = "stock",
) -> int:
    """UPSERT на (asset_id, день). `asset_id` канонічний — для акції це
    тикер, для watchlist-активу його `asset_id` (золото/нафта/BTC
    тикера в цьому сенсі не мають). `notified_at = NULL` при оновленні —
    той самий фікс і причина, що `news_analysis/_db.py:save_synthesis`
    (2026-09-29): інакше оновлений того ж дня висновок назавжди
    лишався б невидимим для notify-скрипта."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fundamental_analysis
                (asset_id, asset_kind, ticker, company_name, direction, confidence,
                 summary, reasoning, strengths, risks, inputs, llm_call_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (asset_id, ((created_at AT TIME ZONE 'UTC')::date)) DO UPDATE SET
                asset_kind = EXCLUDED.asset_kind,
                ticker = EXCLUDED.ticker,
                company_name = EXCLUDED.company_name,
                direction = EXCLUDED.direction,
                confidence = EXCLUDED.confidence,
                summary = EXCLUDED.summary,
                reasoning = EXCLUDED.reasoning,
                strengths = EXCLUDED.strengths,
                risks = EXCLUDED.risks,
                inputs = EXCLUDED.inputs,
                llm_call_id = EXCLUDED.llm_call_id,
                created_at = now(),
                notified_at = NULL
            RETURNING id
            """,
            (
                asset_id, asset_kind, ticker, company_name,
                result.direction, result.confidence,
                result.summary, result.reasoning,
                json.dumps(result.strengths, ensure_ascii=False),
                json.dumps(result.risks, ensure_ascii=False),
                json.dumps(inputs, ensure_ascii=False, default=str),
                llm_call_id,
            ),
        )
        analysis_id = cur.fetchone()[0]
    conn.commit()
    return analysis_id


def fetch_latest_filing_insight(conn, ticker: str):
    """Тези з ТЕКСТУ останнього звіту 10-K/10-Q цього тикера
    (`filing_insights`, 2026-10-13), або None.

    Потрібно `fundamental_llm.build_prompt`, щоб аналіз міг спиратись
    не лише на цифри: живий вивід 12 жовтня показав, що з самих чисел
    причину вивести неможливо — для кожної з 5 акцій писались ті самі
    чотири ризики. Причина написана словами в MD&A."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fi.summary, fi.drivers, fi.risks, fi.changes,
                   fi.direction, fi.confidence,
                   cf.form, cf.filing_date
            FROM filing_insights fi
            JOIN company_filings cf ON cf.id = fi.filing_id
            WHERE fi.ticker = %s
            ORDER BY cf.filing_date DESC
            LIMIT 1
            """,
            (ticker,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [c[0] for c in cur.description]
    return dict(zip(columns, row))


def fetch_company_events(conn, ticker: str, days: int, limit: int) -> list[dict]:
    """8-K за останні `days` днів, найновіші першими (`company_events`,
    2026-10-22)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT filing_date, form, items, url
            FROM company_events
            WHERE ticker = %s AND filing_date >= current_date - %s::int
            ORDER BY filing_date DESC
            LIMIT %s
            """,
            (ticker.upper(), days, limit),
        )
        columns = [c[0] for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def fetch_insider_transactions(conn, ticker: str, days: int) -> list[dict]:
    """Угоди Form 4 за останні `days` днів, найновіші першими."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT transaction_date, filing_date, owner_name, owner_role, security,
                   derivative, code, shares, price, acquired_disposed, owned_after
            FROM insider_transactions
            WHERE ticker = %s AND COALESCE(transaction_date, filing_date) >= current_date - %s::int
            ORDER BY COALESCE(transaction_date, filing_date) DESC, accession, seq
            """,
            (ticker.upper(), days),
        )
        columns = [c[0] for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def fetch_snapshot(conn, ticker: str, sources: tuple[str, ...], fields: tuple[str, ...]) -> dict:
    """{поле: {джерело: (значення, дата)}} — ОСТАННІЙ знімок кожного
    джерела по кожному полю (консенсус аналітиків, free float)."""
    metric_ids = [f"{ticker.lower()}_{field}" for field in fields]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (source, metric_id) source, metric_id, value, observed_at
            FROM v_observations_latest_revision
            WHERE source = ANY(%s) AND metric_id = ANY(%s)
            ORDER BY source, metric_id, observed_at DESC
            """,
            (list(sources), metric_ids),
        )
        rows = cur.fetchall()
    prefix = f"{ticker.lower()}_"
    out: dict[str, dict] = {}
    for source, metric_id, value, observed_at in rows:
        out.setdefault(metric_id[len(prefix):], {})[source] = (value, observed_at)
    return out


def fetch_latest_analyses(conn, asset_ids: list[str], max_age_days: int) -> list[dict]:
    """Останній висновок по кожному з `asset_ids` не старший за
    `max_age_days` — вхід фінального вердикту (2026-10-24). Не лише
    сьогоднішні: актив, чиї входи не змінились (гейт змін), сьогодні
    нового рядка не має, але у вердикті бути мусить."""
    if not asset_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (asset_id)
                   id, asset_id, asset_kind, company_name, direction, confidence,
                   summary, strengths, risks, created_at
            FROM fundamental_analysis
            WHERE asset_id = ANY(%s)
              AND created_at >= now() - make_interval(days => %s)
            ORDER BY asset_id, created_at DESC
            """,
            (list(asset_ids), max_age_days),
        )
        columns = [c[0] for c in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    order = {asset_id: i for i, asset_id in enumerate(asset_ids)}
    return sorted(rows, key=lambda r: order.get(r["asset_id"], len(order)))


def save_verdict(conn, verdict_date, result, analysis_ids: list[int], llm_call_id) -> int:
    """UPSERT на день: повторний прогін того ж дня (ручний) оновлює
    вердикт і знову робить його ненадісланим — той самий принцип, що
    `save_fundamental_analysis`."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fundamental_verdict
                (verdict_date, summary, key_points, conflicts, watch, analysis_ids, llm_call_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (verdict_date) DO UPDATE SET
                summary = EXCLUDED.summary,
                key_points = EXCLUDED.key_points,
                conflicts = EXCLUDED.conflicts,
                watch = EXCLUDED.watch,
                analysis_ids = EXCLUDED.analysis_ids,
                llm_call_id = EXCLUDED.llm_call_id,
                created_at = now(),
                notified_at = NULL
            RETURNING id
            """,
            (
                verdict_date, result.summary,
                json.dumps(result.key_points, ensure_ascii=False),
                json.dumps(result.conflicts, ensure_ascii=False),
                json.dumps(result.watch, ensure_ascii=False),
                json.dumps(analysis_ids),
                llm_call_id,
            ),
        )
        verdict_id = cur.fetchone()[0]
    conn.commit()
    return verdict_id
