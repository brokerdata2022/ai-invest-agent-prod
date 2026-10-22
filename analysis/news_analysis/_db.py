"""
Тонкий SQL-шар для news-аналізу — той самий принцип, що й
analysis/screening/_batch_db.py: SQL без жодної інтерпретації значень,
логіка (виклик DeepSeek, парсинг) лишається в relevance_filter.py/
synthesize.py.
"""

import json
from typing import Optional

from news_analysis.aggregate import AssetSignal, NewsCluster
from news_analysis.prices import PriceChange
from news_analysis.relevance_filter import NewsAnalysisResult

# SynthesisResult (synthesize.py/synthesize_market.py) навмисно НЕ
# імпортується для type hint: обидва модулі імпортують цей модуль
# (fetch_relevant_for_aggregation/save_synthesis/
# save_market_synthesis) — імпорт у зворотному напрямку дав би
# циклічний імпорт. save_synthesis()/save_market_synthesis() приймають
# result качиною типізацією (потрібні лише .direction/.confidence/
# .summary/.reasoning).


def fetch_unanalyzed(conn, stream: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Статті з raw_news, для яких ще немає рядка в news_analysis
    (LEFT JOIN ... IS NULL — не викликаємо DeepSeek на ту саму статтю
    двічі)."""
    query = """
        SELECT n.id, n.source, n.stream, n.title, n.url, n.published_at,
               n.raw_payload
        FROM raw_news n
        LEFT JOIN news_analysis a ON a.raw_news_id = n.id
        WHERE a.id IS NULL
    """
    params: list = []
    if stream is not None:
        query += " AND n.stream = %s"
        params.append(stream)
    query += " ORDER BY n.published_at DESC LIMIT %s"
    params.append(limit)

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def fetch_relevant_for_aggregation(
    conn, stream: Optional[str] = None, max_age_days: int = 7
) -> list[dict]:
    """Релевантні статті за останні max_age_days — вхід для
    aggregate.py (кластеризація дублікатів + зведення по активу).
    Фільтр за n.published_at (коли стаття ВИЙШЛА), не a.created_at
    (коли ми її проаналізували) — той самий принцип свіжості, що й
    reporting/news_notify.py:fetch_recent_relevant() (docs/decisions.md,
    2026-09-26)."""
    query = """
        SELECT n.id AS raw_news_id, n.stream, n.title, n.url, n.published_at,
               a.asset_id, a.direction, a.confidence, a.summary
        FROM news_analysis a
        JOIN raw_news n ON n.id = a.raw_news_id
        WHERE a.is_relevant = true
          AND n.published_at >= now() - (%s || ' days')::interval
    """
    params: list = [max_age_days]
    if stream is not None:
        query += " AND n.stream = %s"
        params.append(stream)
    query += " ORDER BY n.published_at DESC"

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def fetch_consolidated_as_clusters(
    conn,
    streams: tuple[str, ...],
    max_age_hours: int,
    min_sources: int,
    limit: int,
) -> list[dict]:
    """Консолідовані історії як вхід для глобального синтезу.

    **Чому не `aggregate.cluster_articles` (зміна 2026-10-13).** Той
    шлях групує статті за СХОЖІСТЮ ЗАГОЛОВКІВ (SequenceMatcher), і на
    живих даних він не знаходить ДУБЛІКАТІВ ВЗАГАЛІ: замір 13 жовтня —
    19 geopolitical-статей дали 19 кластерів, 20 general дали 20,
    `source_count = 1` у кожного. Через це `top_clusters(limit=8)`
    нічого не ранжував (усі ваги однакові) і просто віддавав перші 8
    у довільному порядку — звідси `cluster_count = 8` у ВСІХ 26
    рядках `market_synthesis` за весь час існування, однакові
    заголовки щодня і `confidence = 0.35` у 14 з 26.

    `news_consolidated` (LLM-дедуп, джоби `news_consolidate_*` +
    `news_merge_similar`) ті самі дублікати знаходить: той самий
    замір дав до 19 джерел на одну історію і 40 підтверджених (2+
    джерела) за 3 дні. Тобто шкала значущості існувала, синтез просто
    читав не з того місця.

    `min_sources` відсікає непідтверджені історії, `limit` лишається
    захистом від роздування промпта — але тепер це СТЕЛЯ, а не
    фіксована кількість: якщо підтверджених історій три, синтез і
    побачить три, а `cluster_count` у БД почне відображати реальність.
    """
    placeholders = ", ".join(["%s"] * len(streams))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT summary, direction, source_count, source_urls,
                   source_raw_news_ids, asset_id, created_at
            FROM news_consolidated
            WHERE stream IN ({placeholders})
              AND created_at >= now() - make_interval(hours => %s)
              AND source_count >= %s
            ORDER BY source_count DESC, created_at DESC
            LIMIT %s
            """,
            # Години, не дні (2026-10-22): сесійний бриф бере лише те,
            # що сталось від попередньої сесії, а не тиждень.
            (*streams, max_age_hours, min_sources, limit),
        )
        columns = [c[0] for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def save_synthesis(
    conn,
    asset_id: str,
    signal: AssetSignal,
    price: PriceChange,
    window_days: int,
    result,  # SynthesisResult (synthesize.py) — качина типізація, див. коментар вище
    llm_call_id: int,
) -> int:
    """UPSERT — один рядок на (asset_id, день): повторний прогін того
    самого активу того самого дня оновлює вже наявний рядок, не
    дублює (db/schema.sql:idx_news_synthesis_asset_per_day).

    `notified_at = NULL` у DO UPDATE (2026-09-29, критичний фікс, живий
    фідбек користувача): без цього актив, уже надісланий сьогодні,
    ставав НАЗАВЖДИ невидимим для notify_synthesis.py при повторному
    синтезі того самого дня — вміст (direction/confidence/summary)
    оновлювався, а `notified_at` лишався зі старого відправлення, тож
    `WHERE notified_at IS NULL` більше ніколи не бачив рядок аж до
    наступної доби.

    `confirmation_factors` (2026-10-02, живий фідбек користувача) —
    окреме поле LLM-висновку (synthesize.py:PriceNewsSynthesisResult):
    конкретні фактори/дані, які підтвердили б чи спростували гіпотезу
    тренд/корекція з summary. Качина типізація нижче (`result.summary`
    тощо) тому й не ловить відсутність цього атрибута статично."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO news_synthesis
                (asset_id, window_days, cluster_count, net_lean, price_pct_change,
                 price_start_date, price_end_date, summary, confirmation_factors,
                 direction, confidence, reasoning, source_refs, llm_call_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (asset_id, ((created_at AT TIME ZONE 'UTC')::date)) DO UPDATE SET
                window_days = EXCLUDED.window_days,
                cluster_count = EXCLUDED.cluster_count,
                net_lean = EXCLUDED.net_lean,
                price_pct_change = EXCLUDED.price_pct_change,
                price_start_date = EXCLUDED.price_start_date,
                price_end_date = EXCLUDED.price_end_date,
                summary = EXCLUDED.summary,
                confirmation_factors = EXCLUDED.confirmation_factors,
                direction = EXCLUDED.direction,
                confidence = EXCLUDED.confidence,
                reasoning = EXCLUDED.reasoning,
                source_refs = EXCLUDED.source_refs,
                llm_call_id = EXCLUDED.llm_call_id,
                created_at = now(),
                notified_at = NULL
            RETURNING id
            """,
            (
                asset_id,
                window_days,
                signal.cluster_count,
                signal.net_lean,
                price.pct_change,
                price.start_date,
                price.end_date,
                result.summary,
                result.confirmation_factors,
                result.direction,
                result.confidence,
                result.reasoning,
                json.dumps(signal.summaries),
                llm_call_id,
            ),
        )
        synthesis_id = cur.fetchone()[0]
    conn.commit()
    return synthesis_id


def save_market_synthesis(
    conn,
    clusters: list[NewsCluster],
    macro: dict,
    window_days: int,
    result,  # SynthesisResult (synthesize_market.py) — качина типізація, див. коментар вище
    llm_call_id: int,
    session: str = "daily",
) -> int:
    """UPSERT — один рядок на (день, СЕСІЯ)
    (db/schema.sql:idx_market_synthesis_per_session).

    `session` з'явився 2026-10-04: було UNIQUE на даті, тобто один
    рядок на добу — три сесійні прогони (спек 2026-09-28) затирали б
    один одного. Дефолт 'daily' лишає сумісність для прогону без
    явної сесії.

    `notified_at = NULL` у DO UPDATE — той самий фікс і причина, що
    save_synthesis() вище (2026-09-29)."""
    source_refs = [{"title": c.representative_title, "source_count": c.source_count} for c in clusters]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market_synthesis
                (window_days, cluster_count, macro_context, summary, direction,
                 confidence, reasoning, source_refs, llm_call_id, session)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (((created_at AT TIME ZONE 'UTC')::date), session) DO UPDATE SET
                window_days = EXCLUDED.window_days,
                cluster_count = EXCLUDED.cluster_count,
                macro_context = EXCLUDED.macro_context,
                summary = EXCLUDED.summary,
                direction = EXCLUDED.direction,
                confidence = EXCLUDED.confidence,
                reasoning = EXCLUDED.reasoning,
                source_refs = EXCLUDED.source_refs,
                llm_call_id = EXCLUDED.llm_call_id,
                created_at = now(),
                notified_at = NULL
            RETURNING id
            """,
            (
                window_days,
                len(clusters),
                json.dumps(macro),
                result.summary,
                result.direction,
                result.confidence,
                result.reasoning,
                json.dumps(source_refs),
                llm_call_id,
                session,
            ),
        )
        synthesis_id = cur.fetchone()[0]
    conn.commit()
    return synthesis_id


def save_analysis(
    conn, raw_news_id: int, result: NewsAnalysisResult, llm_call_id: int
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO news_analysis
                (raw_news_id, asset_id, is_relevant, summary, direction,
                 confidence, reasoning, llm_call_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                raw_news_id,
                result.asset_id,
                result.is_relevant,
                result.summary,
                result.direction,
                result.confidence,
                result.reasoning,
                llm_call_id,
            ),
        )
    conn.commit()


def fetch_session_trading_list(conn, kinds: tuple[str, ...]) -> list[dict]:
    """Активи ОСТАННЬОГО прогону списку для торгів, що торгуються в цю
    сесію (`config.SESSION_ASSET_CLASSES`) — для сесійного брифу
    (2026-10-22, пункт 5 плану сесії). Порожньо — або прогін порожній,
    або в цю сесію торгувати нічим; бриф прямо це скаже."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, label, kind, direction, why, levels, score
            FROM trading_list
            WHERE run_at = (SELECT MAX(run_at) FROM trading_list_runs)
              AND kind = ANY(%s)
            ORDER BY score DESC
            """,
            (list(kinds),),
        )
        columns = [c[0] for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
