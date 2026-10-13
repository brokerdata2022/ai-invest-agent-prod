"""
Тонкий SQL-шар для expectation_comparisons — той самий принцип, що
analysis/news_analysis/_db.py: SQL без інтерпретації значень, уся
логіка обчислення (методи/парсинг) лишається в comparison_methods.py/
parse_expected.py.
"""

import json


def save_comparison(conn, result) -> int:
    """result — compare_releases.ComparisonResult."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO expectation_comparisons
                (release_log_id, source, metric_id, observed_at, actual_value,
                 expected_value_raw, expected_value_parsed, surprise, surprise_pct,
                 comparison_method)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                result.release_log_id,
                result.source,
                result.metric_id,
                result.observed_at,
                result.actual_value,
                result.expected_value_raw,
                result.expected_value_parsed,
                result.surprise,
                result.surprise_pct,
                result.comparison_method,
            ),
        )
        comparison_id = cur.fetchone()[0]
    conn.commit()
    return comparison_id


def mark_notified(conn, comparison_ids: list[int]) -> None:
    """Проставляє notified_at = now() — виклик reporting/expectations_notify.py
    одразу після успішної відправки в Telegram, щоб наступний цикл
    (щохвилини) не надіслав ті самі рядки повторно."""
    if not comparison_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE expectation_comparisons SET notified_at = now() WHERE id = ANY(%s)",
            (comparison_ids,),
        )
    conn.commit()


def mark_impact_notified(conn, synthesis_ids: list[int]) -> None:
    """Проставляє expectation_synthesis.impact_notified_at = now() —
    виклик reporting/release_impact_notify.py одразу після успішної
    відправки. ОКРЕМИЙ дедуп від mark_notified() вище (та окрема
    колонка): 2026-10-02, рішення користувача — короткий сюрприз-звіт
    (expectations_notify.py) і широкий розбір міжактивного впливу
    (release_impact_notify.py) це два незалежні повідомлення з
    незалежними "вже надіслано"."""
    if not synthesis_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE expectation_synthesis SET impact_notified_at = now() WHERE id = ANY(%s)",
            (synthesis_ids,),
        )
    conn.commit()


def fetch_unsynthesized_comparisons(conn, limit: int = 20) -> list[dict]:
    """Рядки expectation_comparisons, для яких ще немає expectation_synthesis
    (LEFT JOIN ... IS NULL — той самий патерн, що news_analysis/_db.py:
    fetch_unanalyzed()). impact_level тягнеться з release_log — потрібен
    LLM для контексту "наскільки взагалі важливий цей реліз"."""
    query = """
        SELECT ec.id AS comparison_id, ec.release_log_id, ec.source, ec.metric_id,
               ec.observed_at, ec.actual_value, ec.expected_value_raw,
               ec.expected_value_parsed, ec.surprise, ec.surprise_pct,
               ec.comparison_method, rl.impact_level
        FROM expectation_comparisons ec
        JOIN release_log rl ON rl.id = ec.release_log_id
        LEFT JOIN expectation_synthesis es ON es.comparison_id = ec.id
        WHERE es.id IS NULL
        ORDER BY ec.created_at ASC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(query, (limit,))
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def save_synthesis(
    conn,
    comparison_id: int,
    result,  # ExpectationSynthesisResult (synthesize.py) — качина
             # типізація: .direction/.confidence/.summary/.reasoning
             # (той самий принцип, що news_analysis/_db.py:save_synthesis())
             # + .impacts (2026-10-02, комплексний міжактивний вплив).
    source_refs: list,
    llm_call_id: int,
) -> int:
    """UPSERT — той самий release_log-цикл не синтезується двічі
    (db/schema.sql: expectation_synthesis.comparison_id UNIQUE), але
    ретрай (ручний повторний прогін) оновлює рядок, не падає на
    конфлікті."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO expectation_synthesis
                (comparison_id, summary, direction, confidence, reasoning,
                 source_refs, asset_impacts, llm_call_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (comparison_id) DO UPDATE SET
                summary = EXCLUDED.summary,
                direction = EXCLUDED.direction,
                confidence = EXCLUDED.confidence,
                reasoning = EXCLUDED.reasoning,
                source_refs = EXCLUDED.source_refs,
                asset_impacts = EXCLUDED.asset_impacts,
                llm_call_id = EXCLUDED.llm_call_id,
                created_at = now()
            RETURNING id
            """,
            (
                comparison_id,
                result.summary,
                result.direction,
                result.confidence,
                result.reasoning,
                json.dumps(source_refs),
                json.dumps(getattr(result, "impacts", [])),
                llm_call_id,
            ),
        )
        synthesis_id = cur.fetchone()[0]
    conn.commit()
    return synthesis_id
