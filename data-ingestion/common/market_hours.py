"""
Чи торгується зараз клас активів — вихідні й біржові свята
(2026-10-24, рішення користувача: "на вихідних, коли торги окрім
крипти закриті, ти шлеш аналізи, які не потрібні на даний час").

Живий кейс того самого дня (субота): 12 синтезів новини+ціна по
акціях, нафті, золоту й USD/JPY, азійський бриф, список для торгів і
watchlist на 11 активів — усе про ринки, що стоять до понеділка.

Три різні питання — три функції:
- `is_open(клас)` — чи СЬОГОДНІ торговий день для класу активів (для
  повідомлень і аналізів протягом дня). Акції/індекси — за датою в поясі
  агента й календарем свят біржі; форекс і товари — ще й за тижневим
  вікном (нд 17:00 → пт 17:00 за Нью-Йорком); крипта — завжди.
- `exchange_session_today(сесія)` — чи є сьогодні торги на КОНКРЕТНІЙ
  біржі за її місцевою датою (сесійні брифи: японські свята не
  збігаються з американськими).
- `sessions_between(клас, від, до)` — скільки торгових днів класу
  минуло між двома моментами (скринінг акцій: субота після п'ятниці —
  нова сесія, понеділок о 5:00 після суботи — ні).

Тут немає аналітики — лише календар, як `freshness.py`; тому модуль у
`common/`, доступний і analysis/, і reporting/, і orchestration/.
"""

from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

import holidays

import config

ALWAYS_OPEN = frozenset({"crypto"})


@lru_cache(maxsize=None)
def _calendar(code: str, year: int):
    return holidays.financial_holidays(code, years=year)


def is_exchange_holiday(code: str, day: date) -> bool:
    return day in _calendar(code, day.year)


def is_trading_day(code: Optional[str], day: date) -> bool:
    """Будній день і не свято біржі `code` (None — лише будні)."""
    if day.weekday() >= 5:
        return False
    return not (code and is_exchange_holiday(code, day))


def _now(at: Optional[datetime]) -> datetime:
    if at is None:
        return datetime.now(ZoneInfo(config.MARKET_HOURS_TIMEZONE))
    if at.tzinfo is None:
        return at.replace(tzinfo=ZoneInfo(config.MARKET_HOURS_TIMEZONE))
    return at


def in_weekly_window(at: Optional[datetime] = None) -> bool:
    """Тижневе вікно форексу й товарних ф'ючерсів: від неділі 17:00 до
    п'ятниці 17:00 за Нью-Йорком."""
    local = _now(at).astimezone(ZoneInfo(config.WEEKLY_MARKET_TIMEZONE))
    open_day, open_hour = config.WEEKLY_MARKET_OPEN
    close_day, close_hour = config.WEEKLY_MARKET_CLOSE
    weekday, hour = local.weekday(), local.hour
    if weekday == 5:                                   # субота — закрито весь день
        return False
    if weekday == open_day:                            # неділя — з години відкриття
        return hour >= open_hour
    if weekday == close_day:                           # п'ятниця — до години закриття
        return hour < close_hour
    return True


def is_open(asset_class: Optional[str], at: Optional[datetime] = None) -> bool:
    """Чи сьогодні торговий день для класу активів. Невідомий клас
    вважається акцією: це найсуворіший календар, тож помилка тут дає
    зайву тишу у вихідні, а не зайве повідомлення."""
    asset_class = asset_class or "stock"
    if asset_class in ALWAYS_OPEN:
        return True
    now = _now(at)
    local_day = now.astimezone(ZoneInfo(config.MARKET_HOURS_TIMEZONE)).date()
    if asset_class in config.WEEKLY_MARKET_CLASSES:
        if not in_weekly_window(now):
            return False
        code = config.MARKET_HOLIDAY_CALENDARS.get(asset_class)
        # Вікно відкривається в неділю ввечері за Нью-Йорком, коли за
        # Києвом уже ніч на понеділок — тож лише свято, а не вихідний,
        # закриває форекс у межах вікна.
        return not (code and is_exchange_holiday(code, local_day))
    return is_trading_day(config.MARKET_HOLIDAY_CALENDARS.get(asset_class, "XNYS"), local_day)


def open_classes(classes: Iterable[str], at: Optional[datetime] = None) -> set[str]:
    return {c for c in classes if is_open(c, at)}


def exchange_session_today(session: str, at: Optional[datetime] = None) -> bool:
    """Чи є сьогодні торги на біржі сесії (`config.SESSION_EXCHANGES`)
    за МІСЦЕВОЮ датою біржі."""
    code, tz = config.SESSION_EXCHANGES[session]
    return is_trading_day(code, _now(at).astimezone(ZoneInfo(tz)).date())


def sessions_between(asset_class: str, since: date, until: date) -> int:
    """Скільки торгових днів класу в проміжку (since, until] — кількість
    НОВИХ сесій, що могли дати нові дані після прогону `since`."""
    code = config.MARKET_HOLIDAY_CALENDARS.get(asset_class, "XNYS")
    count, day = 0, since + timedelta(days=1)
    while day <= until:
        if is_trading_day(code, day):
            count += 1
        day += timedelta(days=1)
    return count


def previous_day_traded(asset_class: str, at: Optional[datetime] = None) -> bool:
    """Чи був УЧОРА (за датою агента) торговий день класу — тобто чи
    з'явились нові дані закриття для ранкового прогону. Субота 05:00 —
    так (п'ятниця), неділя й понеділок 05:00 — ні (субота й неділя),
    ранок після свята — ні. Без пам'яті між прогонами: календар сам
    каже, чи була сесія."""
    local_day = _now(at).astimezone(ZoneInfo(config.MARKET_HOURS_TIMEZONE)).date()
    yesterday = local_day - timedelta(days=1)
    if asset_class in ALWAYS_OPEN:
        return True
    return is_trading_day(config.MARKET_HOLIDAY_CALENDARS.get(asset_class, "XNYS"), yesterday)


def schedule_gate(cfg: dict, at: Optional[datetime] = None) -> Optional[str]:
    """Чи пропустити ЗАПЛАНОВАНИЙ прогін джоби: None — запускати, рядок
    — причина пропуску. Ключі запису `orchestration/schedule.py`:
    - `"markets"` — класи активів; пропуск, якщо ЖОДЕН не торгується;
    - `"session"` — сесійний бриф; пропуск, якщо в біржі сесії сьогодні
      торгів немає (за її місцевою датою);
    - `"after_session"` — клас; пропуск, якщо вчора не було торгового
      дня цього класу (нових даних закриття немає)."""
    markets = cfg.get("markets")
    if markets and not open_classes(markets, at):
        return f"ринки закриті ({', '.join(markets)})"
    session = cfg.get("session")
    if session and not exchange_session_today(session, at):
        code, _tz = config.SESSION_EXCHANGES[session]
        return f"сьогодні немає торгів на {code} (сесія {session})"
    after = cfg.get("after_session")
    if after and not previous_day_traded(after, at):
        return f"учора не було торгового дня ({after}) — нових даних закриття немає"
    return None


def asset_is_open(asset_id: str, classes: dict[str, str], at: Optional[datetime] = None) -> bool:
    """Для змішаних універсумів (watchlist + тикери скринінгу): клас
    береться з `classes` ({asset_id: asset_class}, зазвичай із
    `watchlist_assets`), а все, чого там немає, — тикер акції."""
    return is_open(classes.get(asset_id) or classes.get(asset_id.lower()) or "stock", at)
