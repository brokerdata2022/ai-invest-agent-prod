#!/usr/bin/env python3
"""
Перевіряє, чи настав час уже заведених 'pending' релізів
(`monitoring/refresh_calendar.py` заводить їх наперед) — і для тих, що
настали, АКТИВНО забирає нові дані (не просто чекає, поки вони
з'являться самі).

Розрахований на запуск ЩОХВИЛИНИ (orchestration/schedule.py, 2026-10-02
— раніше був раз на 15 хв + release_log.DEFAULT_BUFFER_MINUTES=30,
живий фідбек користувача: комбінація давала затримку детекції до ~44 хв
навіть коли scheduler живий, хоча точний час релізу вже відомий з
тижневого calendar-seed). Це НЕ цілодобовий моніторинг у сенсі
навантаження: коли pending-рядків, чий scheduled_at уже настав, немає —
прогін робить лише один дешевий SELECT (`get_pending_entries`) і
завершується; реальний виклик зовнішнього адаптера відбувається ЛИШЕ
для рядків, час яких дійсно настав, і повторюється щохвилини, доки
дані не з'являться. На відміну від `refresh_calendar.py` (тижнева
періодичність), тут жодного звернення до FRED-календаря чи
economic_calendar — тільки release_log + повторний виклик уже готового
адаптера (`monitoring/metric_sources.py` — FRED/ECB/BOJ/e-Stat, той
самий шлях, що `run_collect.py`).

Ідемпотентно: якщо дані ще не з'явились (джерело затримало
публікацію) — рядок лишається 'pending', наступний запуск спробує
знову.

Використання:
    python check_releases.py
"""

import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data-ingestion"))

from common.db import get_connection, insert_observations  # noqa: E402
from metric_sources import ADAPTER_BY_METRIC  # noqa: E402
from release_log import get_pending_entries, is_past_buffer, mark_detected  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def try_fetch_one(conn, entry: dict) -> None:
    metric_id = entry["metric_id"]
    adapter_class, api_key_env = ADAPTER_BY_METRIC[metric_id]

    kwargs = {"metric_id": metric_id}
    if api_key_env:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            logger.error("%s: %s не задано — див. .env.example, пропущено", metric_id, api_key_env)
            return
        kwargs["api_key"] = api_key

    adapter = adapter_class(**kwargs)
    records = adapter.collect(limit=5)
    if not records:
        logger.info("%s: адаптер не повернув записів — спробуємо ще раз наступного разу", metric_id)
        return

    inserted = insert_observations(conn, records)
    if inserted > 0:
        mark_detected(conn, entry["id"])
        logger.info("%s: нові дані виявлено (%d записів), позначено detected", metric_id, inserted)
    else:
        logger.info("%s: даних ще немає (0 нових записів) — лишається pending", metric_id)


def main() -> None:
    load_dotenv()

    conn = get_connection()
    try:
        pending = get_pending_entries(conn)
        now = datetime.now(timezone.utc)
        due = [e for e in pending if is_past_buffer(e["scheduled_at"], now)]

        if not due:
            logger.info("Немає pending-релізів, час яких уже настав (%d pending загалом)", len(pending))
            return

        for entry in due:
            try:
                try_fetch_one(conn, entry)
            except Exception:
                # Один показник не повинен валити весь прогін (напр.
                # тимчасовий мережевий збій до джерела) — лишається
                # pending, спробуємо знову наступного разу. rollback()
                # обов'язковий: без нього одна SQL-помилка лишає
                # з'єднання в "aborted transaction" і валить усі
                # наступні показники в тому самому циклі (те саме, що
                # refresh_calendar.py).
                logger.error("%s: помилка під час забору даних — лишається pending", entry["metric_id"], exc_info=True)
                conn.rollback()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
