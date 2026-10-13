"""
Календар релізів US-показників — офіційний графік публікацій (FRED
release API, дзеркалить BLS/BEA/Fed) + власний fallback-список рівня
впливу.

**Live-факт, що вплинув на дизайн:** `nonfarm_payrolls` (PAYEMS) і
`unemployment_rate` (UNRATE) належать ОДНОМУ release_id (50,
"Employment Situation") — той самий звіт BLS, тому той самий графік.

**Навмисно НЕ всі FRED-метрики тут.** Тільки ті, що виходять як
дискретний періодичний звіт (CPI, NFP, GDP, ...). Неперервні щоденні
ринкові ряди (fed_funds_rate, treasury_10y/2y, usdjpy_fx_rate,
wti_crude/brent_crude/eurusd/coffee) — виключено, немає окремої "події
релізу". Єврозона/Японія — окремий шлях, `economic_calendar.py`
(FRED release API покриває тільки US).

Impact-рівні — стандартна ринкова класифікація, стартова точка,
підлягає калібруванню. **Fallback-роль:** `economic_calendar.py` дає
точніший impact і точний час, коли знаходить збіг за назвою —
`IMPACT_LEVELS`/`DEFAULT_RELEASE_TIME_ET` тут — лише коли збігу нема.
Деталі й живі перевірки — `docs/decisions.md`.
"""

import logging
import time as time_module
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests

from config import (
    CALENDAR_FRED_RETRIES,
    CALENDAR_FRED_RETRY_BACKOFF_SECONDS,
    CALENDAR_HORIZON_DAYS,
)

logger = logging.getLogger(__name__)

FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"

# internal metric_id → FRED release_id (перевірено живим /fred/series/release)
RELEASE_IDS: dict[str, int] = {
    "cpi": 10,
    "core_cpi": 10,
    "pce_price_index": 54,
    "nonfarm_payrolls": 50,
    "unemployment_rate": 50,
    "initial_jobless_claims": 180,
    "real_gdp": 53,
    "retail_sales": 9,
    "housing_starts": 27,
    "mortgage_rate_30y": 190,
}

# Стартова класифікація впливу (high/medium/low) — не з офіційного API
# (немає такого), ручна, підлягає калібруванню.
IMPACT_LEVELS: dict[str, str] = {
    "cpi": "high",
    "core_cpi": "high",
    "pce_price_index": "high",
    "nonfarm_payrolls": "high",
    "unemployment_rate": "high",
    "real_gdp": "high",
    "initial_jobless_claims": "medium",
    "retail_sales": "medium",
    "housing_starts": "medium",
    "mortgage_rate_30y": "low",
    # Не-US показники додані 2026-10-13 разом із
    # `official_calendars.py`: доти вони майже ніколи не потрапляли в
    # release_log, тож і impact їм був не потрібен — тепер потрапляють,
    # і без цих рядків `impact_level` виходив NULL, а
    # `expectations_notify.py` фільтрує саме за high/medium, тобто
    # сюрприз по них НІКОЛИ б не дійшов до Telegram.
    #
    # Рівні — стандартна ринкова класифікація, та сама логіка, що для
    # US: рішення по ставці й інфляція — high (рухають і валюту, і
    # дохідності), безробіття єврозони — low (виходить із великим
    # лагом і рідко дає сюрприз).
    "fed_funds_rate": "high",
    "eurozone_deposit_rate": "high",
    "eurozone_hicp": "high",
    "japan_policy_rate": "high",
    "japan_cpi": "medium",
    "eurozone_unemployment_rate": "low",
}


def fetch_release_dates(
    metric_id: str,
    api_key: str,
    limit: int = 40,
    session: Optional[requests.Session] = None,
) -> Any:
    """Сирий виклик FRED release/dates для release_id, що відповідає
    metric_id. `include_release_dates_with_no_data=true` — щоб бачити
    вже заплановані майбутні дати (без цього прапорця FRED інколи
    ховає дати, для яких дані ще не опубліковані).

    limit=40 — запас, щоб охопити і минулі, і майбутні дати в одному
    запиті (перевірено живо 2026-09-26: щотижневі релізи мають ~11+
    вже запланованих майбутніх дат наперед, тому малий limit ризикує
    не захопити потрібну; 40 — з запасом, не точний мінімум)."""
    if metric_id not in RELEASE_IDS:
        raise ValueError(
            f"Немає календаря релізів для metric_id {metric_id!r}. "
            f"Доступні: {sorted(RELEASE_IDS)}"
        )
    params = {
        "release_id": RELEASE_IDS[metric_id],
        "api_key": api_key,
        "file_type": "json",
        "include_release_dates_with_no_data": "true",
        "sort_order": "desc",
        "limit": limit,
    }
    sess = session or requests.Session()
    # Ретрай на 5xx: FRED віддає 502/503 помітно часто (живо 2026-10-05
    # — 502 на двох показниках), а без повтору ОДИН такий код викидає
    # показник із календаря до наступного тижневого прогону, тобто
    # пропущений реліз. 4xx не повторюємо — це наша помилка в запиті,
    # повтор нічого не змінить.
    last_error: Optional[Exception] = None
    for attempt in range(1, CALENDAR_FRED_RETRIES + 1):
        try:
            response = sess.get(FRED_RELEASE_DATES_URL, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status < 500:
                raise
            last_error = exc
        except requests.RequestException as exc:
            last_error = exc
        if attempt < CALENDAR_FRED_RETRIES:
            logger.warning(
                "%s: FRED /release/dates не відповів (%s), спроба %d/%d",
                metric_id, last_error, attempt, CALENDAR_FRED_RETRIES,
            )
            time_module.sleep(CALENDAR_FRED_RETRY_BACKOFF_SECONDS)
    raise last_error  # type: ignore[misc]


def parse_release_dates(raw_response: Any) -> list[date]:
    """Сира відповідь → відсортований (зростання) список дат релізу.
    Чиста функція — без мережі, тестується на фікстурах."""
    entries = raw_response.get("release_dates") if isinstance(raw_response, dict) else None
    if not entries:
        logger.warning(
            "Неочікувана відповідь FRED release/dates — немає release_dates: %r",
            str(raw_response)[:300],
        )
        return []

    dates: list[date] = []
    for entry in entries:
        raw_date = entry.get("date")
        if not raw_date:
            continue
        try:
            dates.append(date.fromisoformat(raw_date))
        except ValueError:
            logger.warning("Не вдалось розпарсити дату релізу: %r", raw_date)
            continue

    dates.sort()
    return dates


def next_scheduled_release(
    dates: list[date],
    today: Optional[date] = None,
    horizon_days: int = CALENDAR_HORIZON_DAYS,
) -> Optional[date]:
    """Найближча ще НЕ минула дата релізу (>= today), АЛЕ лише в межах
    горизонту (`config.CALENDAR_HORIZON_DAYS` — обґрунтування значення
    й живий кейс "9 показників із 15 ніколи не були в release_log"
    там же). Горизонт узагалі потрібен, щоб seed не йшов на fallback
    для місячного релізу, який ще за багато тижнів наперед.

    Повертає None, якщо в межах горизонту немає жодної дати — НОРМАЛЬНО
    (не помилка): `refresh_calendar.py` спробує знову наступного
    тижня, коли дата наблизиться."""
    today = today or datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=horizon_days)
    upcoming = [d for d in dates if today <= d <= horizon]
    return min(upcoming) if upcoming else None


# Типовий час публікації BLS/BEA — 8:30 ET для всіх наших 10
# показників (загальновідомо: CPI/NFP/Unemployment Rate/Jobless
# Claims/GDP/Retail Sales/Housing Starts/PCE — усі о 8:30 ET;
# mortgage_rate_30y, Freddie Mac survey, теж публікується вранці
# четверга). US/Eastern через zoneinfo (не хардкод -04:00/-05:00) —
# коректно враховує перехід EST/EDT.
DEFAULT_RELEASE_TIME_ET = time(8, 30)
_EASTERN = ZoneInfo("America/New_York")

# Типовий час релізу ПО РЕГІОНУ — fallback, коли ні офіційна сторінка,
# ні ForexFactory-фід не дали часу.
#
# ЧОМУ З'ЯВИЛОСЬ (2026-10-13). До цього fallback був один — 8:30 ET,
# бо всі календарні показники були US. Коли `official_calendars.py`
# підключив єврозону й Японію, цей самий час поїхав і на них: японський
# CPI отримав "08:30 ET", тобто 21:30 за Токіо — на 13 годин ПІЗНІШЕ
# за реальну публікацію (08:30 JST). Детекція все одно відбулась би
# (check_releases ходить щохвилини й ловить дані, коли вони
# з'являються), але час у звіті був би неправдою, а тригер — із
# багатогодинним запізненням.
#
# Часи перевірені за офіційними сторінками: ECB публікує статистику
# об 11:00 CET, рішення Ради — 14:15 CET (прес-конференція 14:45);
# Statistics Bureau Японії — 08:30 JST; BOJ оголошує рішення близько
# полудня JST (точний час не фіксований — засідання завершується
# коли завершується).
_REGIONAL_DEFAULTS: dict[str, tuple[time, str]] = {
    # FOMC оголошує рішення о 14:00 ET, прес-конференція о 14:30.
    "fed_funds_rate": (time(14, 0), "America/New_York"),
    "eurozone_hicp": (time(11, 0), "Europe/Berlin"),
    "eurozone_unemployment_rate": (time(11, 0), "Europe/Berlin"),
    "eurozone_deposit_rate": (time(14, 15), "Europe/Berlin"),
    "japan_cpi": (time(8, 30), "Asia/Tokyo"),
    "japan_policy_rate": (time(12, 0), "Asia/Tokyo"),
}


def default_scheduled_datetime(due_date: date, metric_id: Optional[str] = None) -> datetime:
    """Fallback, коли economic_calendar.find_event() не знайшов збігу
    за назвою — типовий час замість повної відсутності часу (дата з
    півночі, як було до 2026-09-26).

    `metric_id` (2026-10-13) обирає РЕГІОНАЛЬНИЙ типовий час; без
    нього — історичний 8:30 ET, правильний для US-показників, яких
    тут більшість."""
    regional = _REGIONAL_DEFAULTS.get(metric_id) if metric_id else None
    if regional is not None:
        local_time, zone = regional
        return datetime.combine(due_date, local_time, tzinfo=ZoneInfo(zone))
    return datetime.combine(due_date, DEFAULT_RELEASE_TIME_ET, tzinfo=_EASTERN)
