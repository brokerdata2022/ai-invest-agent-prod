#!/usr/bin/env python3
"""
Наперед заводить календар релізів на найближчий цикл — ЩОДЕННИЙ
прогін, горизонт `config.CALENDAR_HORIZON_DAYS` (21 день).

Було: тижнево + горизонт 7 днів. Ці два числа стикались без
перекриття, і реліз, що випадав за 8 днів від прогону, чекав до
наступного понеділка. Живий наслідок (аудит 2026-10-11): 9
показників із 15 за два тижні роботи ЖОДНОГО РАЗУ не потрапили в
release_log, зокрема `cpi` і `core_cpi` — рівно ті, де бектест дає
найбільшу перевагу ШІ. Тепер кожен реліз має ~20 спроб завестись
замість однієї.

Два шляхи визначення дати релізу:
1. **FRED-анкоровані** (US, `release_calendar.RELEASE_IDS`) — FRED дає
   авторитетну ДАТУ, `economic_calendar.py` (ForexFactory) ЗБАГАЧУЄ
   часом/impact/forecast; немає збігу — fallback (типовий час 8:30 ET).
2. **Календар-only** (eurozone_*/japan_*, `metric_sources.CALENDAR_ONLY_METRICS`
   — ECB/BOJ/e-Stat не мають перевіреного графіка релізів) —
   `economic_calendar.py` — ЄДИНЕ джерело і дати, і часу/impact/forecast
   (`find_upcoming_event()`). Немає збігу цього тижня — просто
   пропускаємо, без fallback (немає іншого джерела дати).

Якщо цей цикл (за датою) ще не заведено в release_log — вставляє
'pending'. Уже заведено, але ще без forecast (фід ще не "перекотився"
на потрібний тиждень) — донасичує (`update_enrichment()`).

Не забирає самі дані показника — тільки календар. Забір даних після
настання часу — `monitoring/check_releases.py` (окремий, частіший
запуск). Деталі й живі перевірки — `docs/decisions.md`.

Використання:
    python refresh_calendar.py
    python refresh_calendar.py --metric cpi
    python refresh_calendar.py --metric eurozone_hicp
"""

import argparse
import logging
import os
import sys
from datetime import date, datetime
from typing import Optional

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data-ingestion"))

from config import CALENDAR_HORIZON_DAYS  # noqa: E402

from common.db import get_connection  # noqa: E402
from economic_calendar import fetch_calendar, find_event, find_upcoming_event  # noqa: E402
from metric_sources import ADAPTER_BY_METRIC, CALENDAR_ONLY_METRICS  # noqa: E402
from official_calendars import fetch_official_dates  # noqa: E402
from speeches import save_speeches  # noqa: E402
from release_calendar import (  # noqa: E402
    IMPACT_LEVELS,
    default_scheduled_datetime,
    fetch_release_dates,
    next_scheduled_release,
    parse_release_dates,
)
from release_log import get_latest_log_entry, insert_pending, should_seed_new_cycle, update_enrichment  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _resolve_scheduled_fields(metric_id: str, event: Optional[dict], due_date: date):
    """event із economic_calendar.find_event() → (scheduled_at, impact,
    expected_value); event=None → fallback (типовий час + власний
    IMPACT_LEVELS). Спільна логіка для seed і для донасичення нижче."""
    if event is not None:
        scheduled_at = datetime.fromisoformat(event["date"])
        impact = event["impact"].lower() if event.get("impact") in ("High", "Medium", "Low") else IMPACT_LEVELS.get(metric_id)
        expected_value = event.get("forecast") or None
        return scheduled_at, impact, expected_value
    return default_scheduled_datetime(due_date, metric_id), IMPACT_LEVELS.get(metric_id), None


def _seed_or_enrich(conn, source: str, metric_id: str, due_date: date, event: Optional[dict]) -> None:
    """Спільний хвіст для обох шляхів (FRED-анкорований і
    календар-only): маючи due_date і (опційно) event з economic_calendar,
    вирішує seed / донасичення / без змін."""
    scheduled_at, impact, expected_value = _resolve_scheduled_fields(metric_id, event, due_date)

    existing = get_latest_log_entry(conn, source, metric_id)
    existing_scheduled_at = existing["scheduled_at"] if existing else None

    if not should_seed_new_cycle(existing_scheduled_at, due_date):
        # Уже заведено цей цикл — але якщо тоді ForexFactory-фід ще не
        # мав збігу (live-факт 2026-09-26: "thisweek" не перекочується
        # заздалегідь) і статус усе ще pending, донасичуємо зараз, коли
        # фід, можливо, уже оновився.
        if existing["status"] == "pending" and not existing["expected_value"] and event is not None:
            update_enrichment(conn, existing["id"], scheduled_at, impact, expected_value)
        else:
            logger.info("%s: цикл %s уже заведено (id=%s, %s) — без змін", metric_id, due_date, existing["id"], existing["status"])
        return

    if event is not None:
        logger.info("%s: збіг у economic_calendar — %s, impact=%s, forecast=%r", metric_id, scheduled_at, impact, expected_value)
    else:
        logger.info("%s: немає збігу в economic_calendar — типовий час %s", metric_id, scheduled_at)

    insert_pending(conn, source, metric_id, scheduled_at, impact, expected_value)


def refresh_fred_anchored(conn, api_key: str, metric_id: str, calendar_events) -> None:
    raw = fetch_release_dates(metric_id, api_key=api_key)
    dates = parse_release_dates(raw)
    due_date = next_scheduled_release(dates)
    if due_date is None:
        logger.info(
            "%s: наступний реліз ще не в межах горизонту (%d днів) — пропущено, спробуємо наступного прогону",
            metric_id, CALENDAR_HORIZON_DAYS,
        )
        return

    event = find_event(metric_id, calendar_events, due_date)
    source = ADAPTER_BY_METRIC[metric_id][0].source
    _seed_or_enrich(conn, source, metric_id, due_date, event)


def refresh_calendar_only(conn, metric_id: str, calendar_events) -> None:
    """Показники без FRED-календаря (єврозона/Японія).

    **Порядок джерел змінено 2026-10-13.** Було: ForexFactory-фід —
    ЄДИНЕ джерело і дати, і часу. Фід покриває лише поточний тиждень,
    тож на практиці це означало "дати майже ніколи немає": за весь час
    роботи агента `eurozone_deposit_rate`, `japan_cpi` і
    `japan_policy_rate` не потрапили в `release_log` ЖОДНОГО РАЗУ.

    Тепер той самий розподіл ролей, що вже був для US: ОФІЦІЙНЕ
    джерело дає ДАТУ (`official_calendars.py` — статистичний календар
    ECB, календар засідань Ради ECB, розклад засідань BOJ, розклад
    публікацій CPI Японії), ForexFactory ЗБАГАЧУЄ часом/impact/
    forecast, якщо збіг знайдеться.

    ForexFactory лишається РЕЗЕРВОМ дати: якщо офіційна сторінка
    зламалась (це HTML-скрейпінг, не API) — працює старий шлях."""
    source = ADAPTER_BY_METRIC[metric_id][0].source

    official_due_date = None
    try:
        official_dates = fetch_official_dates(metric_id)
    except Exception:
        # Зламана сторінка ОДНОГО джерела не повинна зупиняти решту
        # показників — тому лог і перехід на резерв, а не підняття.
        logger.error(
            "%s: офіційний календар недоступний/змінив формат — пробуємо ForexFactory",
            metric_id, exc_info=True,
        )
        official_dates = None

    if official_dates:
        official_due_date = next_scheduled_release(official_dates)

    if official_due_date is not None:
        event = find_event(metric_id, calendar_events, official_due_date)
        logger.info(
            "%s: дата з офіційного календаря — %s%s",
            metric_id, official_due_date,
            " (+ збагачення з economic_calendar)" if event else " (без збігу у фіді: час/impact — fallback)",
        )
        _seed_or_enrich(conn, source, metric_id, official_due_date, event)
        return

    event = find_upcoming_event(metric_id, calendar_events)
    if event is None:
        logger.info(
            "%s: ні офіційного календаря, ні збігу в economic_calendar у межах "
            "горизонту — пропущено", metric_id,
        )
        return

    due_date = datetime.fromisoformat(event["date"]).date()
    logger.info("%s: дата з ForexFactory-фіду (резерв) — %s", metric_id, due_date)
    _seed_or_enrich(conn, source, metric_id, due_date, event)


def main() -> None:
    load_dotenv()

    all_metrics = sorted(ADAPTER_BY_METRIC)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--metric", choices=all_metrics, default=None,
        help="оновити тільки один metric_id; за замовчуванням — усі",
    )
    args = parser.parse_args()

    metric_ids = [args.metric] if args.metric else all_metrics
    needs_fred = any(m not in CALENDAR_ONLY_METRICS for m in metric_ids)

    api_key = os.environ.get("FRED_API_KEY")
    if needs_fred and not api_key:
        logger.error("FRED_API_KEY не задано — див. .env.example.")
        sys.exit(1)

    try:
        calendar_events = fetch_calendar()
    except Exception:
        logger.warning("economic_calendar недоступний — FRED-метрики впадуть на типовий час, eurozone_*/japan_* пропущено цей раз", exc_info=True)
        calendar_events = []

    conn = get_connection()
    try:
        # Виступи членів ЦБ із того самого фіду (2026-10-13). Доти
        # вони приходили й ПРОЛІТАЛИ МИМО: цикл нижче шукає у фіді
        # лише збіги з нашими 15 показниками, а виступи — не релізи
        # даних, тож відкидались. Живий замір: 31 виступ зі 110 подій.
        # Потрібні для повного синтезу ставки (постановка користувача
        # 2026-10-11: "+ виступи банків та голів комісій").
        try:
            save_speeches(conn, calendar_events)
        except Exception:
            logger.error("Збереження виступів провалилось — календар релізів це не блокує", exc_info=True)
            conn.rollback()

        for metric_id in metric_ids:
            try:
                if metric_id in CALENDAR_ONLY_METRICS:
                    refresh_calendar_only(conn, metric_id, calendar_events)
                else:
                    refresh_fred_anchored(conn, api_key, metric_id, calendar_events)
            except Exception:
                # Одна метрика не повинна валити весь прогін (напр.
                # тимчасовий DNS/мережевий збій до зовнішнього API) —
                # логуємо і йдемо далі, той самий rule, що для інших
                # зовнішніх джерел проєкту (retry-with-backoff у GDELT
                # тощо). rollback() обов'язковий: psycopg2 без autocommit
                # лишає з'єднання в "aborted transaction" після будь-якої
                # SQL-помилки — без відкату ВСІ наступні метрики в цьому
                # циклі теж почали б падати через те саме з'єднання.
                logger.error("%s: помилка під час оновлення календаря — пропущено", metric_id, exc_info=True)
                conn.rollback()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
