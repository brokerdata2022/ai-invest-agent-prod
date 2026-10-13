"""
Сесійні синтези мусять спрацьовувати через фіксований час після
ВІДКРИТТЯ РИНКУ — і влітку, і взимку.

Навіщо цей тест (питання користувача 2026-10-04 "ти врахував зміни
літній/зимовий час?"): перша версія стояла в київському часі й була
неправильною. Японія DST не має взагалі, тож Токіо в київському часі
"пливе" на годину на ВСЮ зиму; США переходять в інші дати, ніж ЄС,
тож Нью-Йорк пливе ~3 тижні навесні. Тест рахує РЕАЛЬНИЙ момент
спрацювання в UTC для двох дат — літньої та зимової — і звіряє його з
відкриттям ринку в UTC на ту саму дату.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

from schedule import SCHEDULE, TIMEZONE

# (джоба, пояс ринку, година:хвилина відкриття в локальному часі ринку)
SESSIONS = [
    ("market_synthesis_asia", "Asia/Tokyo", (9, 0)),
    ("market_synthesis_europe", "Europe/London", (8, 0)),
    ("market_synthesis_us", "America/New_York", (9, 30)),
]

# Літня й зимова дати, обрані так, щоб покрити й розбіжність переходів
# США та ЄС: 2026-07-15 — усі на DST; 2026-01-15 — усі на стандартному.
SUMMER = datetime(2026, 7, 15, 0, 0)
WINTER = datetime(2026, 1, 15, 0, 0)

EXPECTED_LAG_MINUTES = 20


def _next_fire_utc(job_name: str, after_local_midnight: datetime) -> datetime:
    """Момент наступного спрацювання джоби в UTC."""
    cfg = SCHEDULE[job_name]
    tz = ZoneInfo(cfg.get("timezone", TIMEZONE))
    trigger = CronTrigger(**cfg["trigger"], timezone=tz)
    start = after_local_midnight.replace(tzinfo=tz)
    fire = trigger.get_next_fire_time(None, start)
    assert fire is not None, f"{job_name}: тригер не дав наступного спрацювання"
    return fire.astimezone(ZoneInfo("UTC"))


def _market_open_utc(market_tz: str, open_hm: tuple[int, int], day: datetime) -> datetime:
    hour, minute = open_hm
    local = day.replace(hour=hour, minute=minute, tzinfo=ZoneInfo(market_tz))
    return local.astimezone(ZoneInfo("UTC"))


@pytest.mark.parametrize("job_name,market_tz,open_hm", SESSIONS)
@pytest.mark.parametrize("day,label", [(SUMMER, "літо"), (WINTER, "зима")])
def test_session_job_fires_fixed_lag_after_market_open(
    job_name, market_tz, open_hm, day, label
):
    fire_utc = _next_fire_utc(job_name, day)
    open_utc = _market_open_utc(market_tz, open_hm, day)

    lag_minutes = (fire_utc - open_utc).total_seconds() / 60
    assert lag_minutes == EXPECTED_LAG_MINUTES, (
        f"{job_name} ({label}): спрацювання {fire_utc:%H:%M} UTC, "
        f"відкриття ринку {open_utc:%H:%M} UTC — зсув {lag_minutes:+.0f} хв "
        f"замість {EXPECTED_LAG_MINUTES}"
    )


def test_tokyo_open_shifts_in_kyiv_time_but_not_in_market_time():
    """Суть помилки, яку виправили: відкриття Токіо в КИЇВСЬКОМУ часі
    різне влітку й узимку (бо Японія DST не має, а Київ має), тож
    фіксована київська година неминуче пливла б."""
    kyiv = ZoneInfo(TIMEZONE)
    summer_kyiv = _market_open_utc("Asia/Tokyo", (9, 0), SUMMER).astimezone(kyiv)
    winter_kyiv = _market_open_utc("Asia/Tokyo", (9, 0), WINTER).astimezone(kyiv)

    assert summer_kyiv.hour != winter_kyiv.hour, (
        "Якщо відкриття Токіо в київському часі однакове, цей тест "
        "більше не описує реальність — перевірити правила DST"
    )


@pytest.mark.parametrize("job_name,market_tz,_open", SESSIONS)
def test_session_jobs_declare_market_timezone(job_name, market_tz, _open):
    """Явна перевірка, що пояс саме ринковий: без цього наступна правка
    могла б тихо повернути джобу в київський час."""
    assert SCHEDULE[job_name].get("timezone") == market_tz


def test_notify_runs_after_latest_possible_synthesis():
    """ОДНА notify-джоба лишається в київському часі, тож її години
    мусять бути після синтезу в ЛЮБОМУ DST-режимі."""
    kyiv = ZoneInfo(TIMEZONE)
    notify_minute = SCHEDULE["notify_market_synthesis"]["trigger"]["minute"]
    notify_hours = {
        int(h) for h in str(SCHEDULE["notify_market_synthesis"]["trigger"]["hour"]).split(",")
    }

    for job_name, market_tz, open_hm in SESSIONS:
        for day in (SUMMER, WINTER):
            fire_kyiv = _next_fire_utc(job_name, day).astimezone(kyiv)
            later = [
                h for h in notify_hours
                if (h, notify_minute) > (fire_kyiv.hour, fire_kyiv.minute)
            ]
            assert later, (
                f"{job_name}: синтез о {fire_kyiv:%H:%M} Києва, а всі години "
                f"notify ({sorted(notify_hours)}) раніші — повідомлення "
                f"пішло б ДО синтезу"
            )
