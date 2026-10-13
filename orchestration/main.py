#!/usr/bin/env python3
"""
Точка входу контейнера `scheduler` (docker-compose.yml): піднімає
APScheduler і тримає процес живим. Розклад — schedule.py, що саме
виконати — jobs.py. Сам нічого не вирішує (orchestration/CLAUDE.md).

Перед стартом (check_startup_gap, 2026-10-02) звіряє heartbeat
(scheduler_heartbeat, jobs.py:_scheduler_heartbeat) з поточним часом —
якщо процес був недоступний ненормально довго (контейнер/хост не
піднятий), шле один Telegram-алерт одразу при відновленні, замість
того щоб користувач дізнавався про причину запізнілого звіту лише
постфактум розслідуванням логів (docs/decisions.md, P0
"Heartbeat планувальника").

Використання:
    python orchestration/main.py
"""

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data-ingestion"))

from alerts import notify_scheduler_gap  # noqa: E402
from common.db import get_connection  # noqa: E402
from jobs import JOBS  # noqa: E402
from runner import run_job  # noqa: E402
from schedule import SCHEDULE, TIMEZONE  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# > інтервал scheduler_heartbeat (5 хв, schedule.py) із запасом на
# misfire_grace_time (300с, build_scheduler нижче) і звичайний
# docker compose restart/перезапуск заради зміни коду (секунди-десятки
# секунд, orchestration/CLAUDE.md) — щоб жоден із цих НОРМАЛЬНИХ
# випадків не виглядав як справжній простій (docs/decisions.md,
# P0 "Heartbeat планувальника").
GAP_ALERT_THRESHOLD = timedelta(minutes=20)


def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=TIMEZONE)
    for job_name, cfg in SCHEDULE.items():
        if job_name not in JOBS:
            raise KeyError(f"schedule.py має джобу {job_name!r}, якої немає в jobs.py:JOBS")
        # Часовий пояс — з джоби, якщо задано, інакше глобальний.
        # Потрібно для сесійних джоб (2026-10-04, питання користувача
        # "ти врахував зміни літній/зимовий час?"): ринок відкривається
        # у ФІКСОВАНИЙ ЛОКАЛЬНИЙ час, а не у фіксований київський, і
        # різні ринки живуть у різних DST-режимах — Японія переходу не
        # має взагалі, США переходять в інші дати, ніж ЄС. Детально —
        # schedule.py, блок market_synthesis_*.
        job_timezone = cfg.get("timezone", TIMEZONE)
        trigger = CronTrigger(**cfg["trigger"], timezone=job_timezone)
        scheduler.add_job(
            run_job,
            trigger=trigger,
            args=[job_name],
            id=job_name,
            max_instances=1,
            misfire_grace_time=300,
        )
        logger.info(
            "Зареєстровано джобу %s: %s [tz=%s] (%s)",
            job_name, cfg["trigger"], job_timezone, cfg["why"],
        )
    return scheduler


def check_startup_gap() -> None:
    """При старті процесу порівнює час останнього heartbeat-тика
    (jobs.py:_scheduler_heartbeat, кожні 5 хв) з поточним часом: розрив
    більший за GAP_ALERT_THRESHOLD означає, що контейнер/хост був
    недоступний ненормально довго — один Telegram-алерт одразу при
    відновленні пояснює користувачу ЗАЗДАЛЕГІДЬ, чому звіти за цей час
    прийдуть із запізненням (живий випадок 2026-10-02: реліз о 15:30
    опрацьовано лише о 17:57 через непомічену ~11-год простою — до
    цієї зміни користувач дізнавався про причину лише розслідуванням
    логів постфактум)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT last_tick_at FROM scheduler_heartbeat WHERE id = 1")
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        logger.info("scheduler_heartbeat ще порожній (перший запуск) — нема з чим звіряти")
        return

    last_tick_at = row[0]
    now = datetime.now(timezone.utc)
    gap = now - last_tick_at
    if gap > GAP_ALERT_THRESHOLD:
        logger.warning("Планувальник був недоступний %s (з %s до %s)", gap, last_tick_at, now)
        notify_scheduler_gap(last_tick_at, now, gap)
    else:
        logger.info("Розрив з останнім heartbeat — %s, у межах норми", gap)


def main() -> None:
    load_dotenv()
    logger.info("Часовий пояс планувальника: %s", TIMEZONE)
    check_startup_gap()
    scheduler = build_scheduler()
    logger.info("Стартую APScheduler з %d джобами", len(SCHEDULE))
    scheduler.start()


if __name__ == "__main__":
    main()
