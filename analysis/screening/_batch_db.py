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

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# Джерела, по яких звіт про відсіяне межею дати вже виходив у ЦЬОМУ
# процесі. Потрібен, бо сам звіт робить запит БЕЗ межі дати — тобто той
# самий дорогий шлях, який оптимізація й прибирає. Живий замір
# 2026-10-16: tier_b робить 6 викликів на `sec_edgar`, і 6 звітів
# повернули прогін із 4.6 с назад до 9.1 с. Раз на джерело досить:
# перелік застарілих рядів у межах одного прогону не змінюється.
_age_report_done: set = set()


def calendar_days_for(trading_points: int, buffer_days: int) -> int:
    """N ТОРГОВИХ точок → календарні дні з запасом.

    Межа дати задається в календарних днях, а вікна скринінгу — у
    торгових (`LOOKBACK_DAYS = 63` це ~3 торгових місяці). Перевести
    треба з запасом: вихідні дають ~1.4×, а святкові кластери — ще
    до двох неторгових тижнів поспіль. Недорахувати тут означає
    молча обрізати ряд і зіпсувати тренд, тому запас явний
    (`config.SCREENING_PRICE_CALENDAR_BUFFER_DAYS`)."""
    return int(trading_points * 7 / 5) + buffer_days


def _cutoff(max_age_days: Optional[int]) -> Optional[date]:
    """Літеральна дата для межі, або None.

    ЛІТЕРАЛЬНА, не `current_date`: остання — STABLE-функція, і
    TimescaleDB не відсікає по ній чанки на ПЛАНУВАННІ. Зміряно
    2026-10-15 на ф'ючерсній історії: 1877 мс → 13.5 мс саме від цієї
    заміни."""
    if max_age_days is None:
        return None
    return date.today() - timedelta(days=max_age_days)


def batch_latest_values(
    conn, source: str, metric_ids: list[str], max_age_days: Optional[int] = None
) -> dict[str, Decimal]:
    """Останнє значення (v_current_values) для СПИСКУ metric_id одним
    запитом. Повертає {metric_id: value}; metric_id без жодного
    запису в БД просто відсутній у результаті (як і раніше:
    відсутність — None для викликача).

    `max_age_days` — не дивитись глибше за N днів. Зміряно на
    продакшені (512 тикерів): **826 мс → 112 мс** із межею 90 днів,
    бо без неї планувальник розглядає всі 1211 чанків. `None`
    (дефолт) лишає стару поведінку — щоб жоден наявний виклик не
    змінив результат непомітно."""
    if not metric_ids:
        return {}
    cutoff = _cutoff(max_age_days)
    with conn.cursor() as cur:
        if cutoff is None:
            cur.execute(
                "SELECT metric_id, value FROM v_current_values "
                "WHERE source = %s AND metric_id = ANY(%s)",
                (source, list(metric_ids)),
            )
        else:
            # DISTINCT ON замість v_current_values: сама в'юшка не має
            # межі по даті, тож фільтр ПОВЕРХ неї не допоміг би
            # планувальнику — предикат мусить стояти на самій
            # hypertable.
            cur.execute(
                """
                SELECT DISTINCT ON (metric_id) metric_id, value
                FROM v_observations_latest_revision
                WHERE source = %s AND metric_id = ANY(%s) AND observed_at >= %s
                ORDER BY metric_id, observed_at DESC
                """,
                (source, list(metric_ids), cutoff),
            )
        return {metric_id: value for metric_id, value in cur.fetchall()}


def batch_series(
    conn,
    source: str,
    metric_ids: list[str],
    limit_per_metric: Optional[int] = None,
    max_age_days: Optional[int] = None,
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
    SEC EDGAR: TTM/YoY шукають "запис ~рік тому").

    `max_age_days` — не дивитись глибше за N днів (`config` розділ 26).
    Потрібне саме для планувальника: без межі він розглядає всі 1211
    чанків, бо `sec_edgar`-історія тягнеться з 2006 року. `None`
    (дефолт) лишає стару поведінку.
    """
    if not metric_ids:
        return {}
    cutoff = _cutoff(max_age_days)
    age_clause = " AND observed_at >= %s" if cutoff is not None else ""
    age_params = (cutoff,) if cutoff is not None else ()
    with conn.cursor() as cur:
        if limit_per_metric is None:
            cur.execute(
                "SELECT metric_id, observed_at, value FROM v_observations_latest_revision "
                f"WHERE source = %s AND metric_id = ANY(%s){age_clause} "
                "ORDER BY metric_id, observed_at DESC",
                (source, list(metric_ids), *age_params),
            )
        else:
            cur.execute(
                f"""
                WITH ranked AS (
                    SELECT metric_id, observed_at, value,
                           ROW_NUMBER() OVER (
                               PARTITION BY metric_id ORDER BY observed_at DESC
                           ) AS rn
                    FROM v_observations_latest_revision
                    WHERE source = %s AND metric_id = ANY(%s){age_clause}
                )
                SELECT metric_id, observed_at, value FROM ranked
                WHERE rn <= %s
                ORDER BY metric_id, observed_at DESC
                """,
                (source, list(metric_ids), *age_params, limit_per_metric),
            )

        result: dict[str, list[tuple]] = {}
        for metric_id, observed_at, value in cur.fetchall():
            result.setdefault(metric_id, []).append((observed_at, value))

    if cutoff is not None:
        _report_excluded_by_age(conn, source, metric_ids, result, cutoff)
    return result


def _report_excluded_by_age(
    conn, source: str, metric_ids: list[str], result: dict, cutoff: date
) -> None:
    """Голосно сказати, що межа дати ВІДСІЯЛА, а не тихо повернути
    порожньо (CLAUDE.md: дефект тут майже ніколи не падає — він тихо
    вертає порожній список і виглядає як робочий код).

    Живий привід (2026-10-16): межа дати на фундаменталі прибрала HAL
    зі скринінгу, і спершу це виглядало як регресія оптимізації.
    Насправді в `hal_revenue` найновіша точка — **2011-06-30**, тобто
    доти скринінг рахував зростання виручки по даних 15-річної
    давності. Таких тикерів 31 із 469. Тепер кожне таке відсіювання
    видно в лозі — інакше наступний такий випадок знову читався б як
    збій оптимізації."""
    if source in _age_report_done:
        return
    missing = [m for m in metric_ids if m not in result]
    if not missing:
        return
    _age_report_done.add(source)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT metric_id, max(observed_at)
            FROM v_observations_latest_revision
            WHERE source = %s AND metric_id = ANY(%s)
            GROUP BY metric_id
            """,
            (source, missing),
        )
        stale = {metric_id: last for metric_id, last in cur.fetchall()}

    if not stale:
        # Даних немає взагалі — це не межа дати, і скаржитись на неї
        # було б хибним слідом.
        return

    examples = ", ".join(
        f"{metric_id} (остання {last})"
        for metric_id, last in sorted(stale.items())[:5]
    )
    logger.warning(
        "%s: %d рядів відсіяно межею дати (дані є, але старші за %s): %s%s",
        source, len(stale), cutoff, examples,
        "…" if len(stale) > 5 else "",
    )
