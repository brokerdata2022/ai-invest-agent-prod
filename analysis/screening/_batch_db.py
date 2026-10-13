"""Пакетні (batch) SQL-запити, спільні для tier_a.py/tier_b.py/tier_c.py.

Чому це окремий модуль: до 2026-09-25 кожен tier-файл ходив у БД
ОКРЕМИМ запитом на кожен тикер (N+1 query pattern) — на живому
universe (~491-411 тикерів) це виливалось у 1500-2500+ послідовних
запитів і хвилини на один tier. Причина повільності кожного окремого
запиту — не розмір результату (найчастіше кілька-сотні рядків), а
те, що v_current_values/v_observations_latest_revision — DISTINCT
ON-views над TimescaleDB hypertable з дрібним (дефолтним)
chunk_time_interval: для тикерів з десятиліттями історії (Twelve
Data) це тисячі чанків, і кожен окремий запит без обмеження по
observed_at змушений перевірити кожен чанк — фіксовані накладні
витрати ~100-150мс НЕЗАЛЕЖНО від розміру результату. N запитів
платять ці накладні витрати N разів; один batched запит (WHERE
metric_id = ANY(список)) платить їх один раз для всього списку
тикерів одразу.

Функції тут — тонка SQL-обгортка, без жодної інтерпретації значень
(CLAUDE.md, правило розділення "збір" vs "аналіз" стосується
data-ingestion/, але той самий принцип — не змішувати SQL з логікою
скринінгу — витримано і тут: обидві функції повертають сирі значення/
серії, вся логіка порогів лишається в чистих функціях passes_tier_*).
"""

from decimal import Decimal
from typing import Optional


def batch_latest_values(conn, source: str, metric_ids: list[str]) -> dict[str, Decimal]:
    """Останнє значення (v_current_values) для СПИСКУ metric_id одним
    запитом. Повертає {metric_id: value}; metric_id без жодного
    запису в БД просто відсутній у результаті (як і раніше:
    відсутність — None для викликача)."""
    if not metric_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT metric_id, value FROM v_current_values "
            "WHERE source = %s AND metric_id = ANY(%s)",
            (source, list(metric_ids)),
        )
        return {metric_id: value for metric_id, value in cur.fetchall()}


def batch_series(
    conn, source: str, metric_ids: list[str], limit_per_metric: Optional[int] = None
) -> dict[str, list[tuple]]:
    """Часовий ряд (v_observations_latest_revision, спадання дати) для
    СПИСКУ metric_id одним запитом. Повертає {metric_id: [(observed_at,
    value), ...]}; metric_id без жодного запису просто відсутній
    (як і в _series()/tier_b.py — виклик .get(metric_id, []) на боці
    викликача).

    limit_per_metric — якщо задано, лишає тільки N найновіших записів
    НА КОЖЕН metric_id (через ROW_NUMBER, а не Python-зрізом після
    вибірки всієї історії — критично для twelvedata, де історія
    сягає десятиліть). Без обмеження (за замовчуванням) — уся
    історія, як і в оригінальному _series()/tier_b.py (потрібно для
    SEC EDGAR: TTM/YoY шукають "запис ~рік тому", межі невідомі наперед).
    """
    if not metric_ids:
        return {}
    with conn.cursor() as cur:
        if limit_per_metric is None:
            cur.execute(
                "SELECT metric_id, observed_at, value FROM v_observations_latest_revision "
                "WHERE source = %s AND metric_id = ANY(%s) "
                "ORDER BY metric_id, observed_at DESC",
                (source, list(metric_ids)),
            )
        else:
            cur.execute(
                """
                WITH ranked AS (
                    SELECT metric_id, observed_at, value,
                           ROW_NUMBER() OVER (
                               PARTITION BY metric_id ORDER BY observed_at DESC
                           ) AS rn
                    FROM v_observations_latest_revision
                    WHERE source = %s AND metric_id = ANY(%s)
                )
                SELECT metric_id, observed_at, value FROM ranked
                WHERE rn <= %s
                ORDER BY metric_id, observed_at DESC
                """,
                (source, list(metric_ids), limit_per_metric),
            )

        result: dict[str, list[tuple]] = {}
        for metric_id, observed_at, value in cur.fetchall():
            result.setdefault(metric_id, []).append((observed_at, value))
        return result
