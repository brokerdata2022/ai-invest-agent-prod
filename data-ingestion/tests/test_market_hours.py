"""
Години ринків: вихідні й свята (2026-10-24). Живий кейс, від якого все
почалось, — субота 24.10.2026: синтези по акціях/нафті/золоту/USD/JPY,
азійський бриф і список для торгів на закриті ринки.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from common import market_hours

KYIV = ZoneInfo("Europe/Kyiv")


def kyiv(y, m, d, h=9, minute=0):
    return datetime(y, m, d, h, minute, tzinfo=KYIV)


@pytest.mark.parametrize("asset_class", ["stock", "index", "fx", "commodity"])
def test_saturday_and_sunday_are_closed_for_everything_but_crypto(asset_class):
    assert not market_hours.is_open(asset_class, kyiv(2026, 10, 24))
    assert not market_hours.is_open(asset_class, kyiv(2026, 10, 25))


def test_crypto_is_always_open():
    assert market_hours.is_open("crypto", kyiv(2026, 10, 24))
    assert market_hours.is_open("crypto", kyiv(2026, 12, 25))


@pytest.mark.parametrize("asset_class", ["stock", "fx", "commodity"])
def test_weekdays_are_open(asset_class):
    assert market_hours.is_open(asset_class, kyiv(2026, 10, 23))
    assert market_hours.is_open(asset_class, kyiv(2026, 10, 26))


def test_us_holiday_closes_stocks_but_not_fx():
    """День подяки: NYSE закрита, форекс торгується."""
    thanksgiving = kyiv(2026, 11, 26, 12)
    assert not market_hours.is_open("stock", thanksgiving)
    assert market_hours.is_open("fx", thanksgiving)


def test_unknown_class_falls_back_to_strictest_calendar():
    """Помилка класу дає зайву тишу у вихідні, а не зайве повідомлення."""
    assert not market_hours.is_open(None, kyiv(2026, 10, 24))
    assert not market_hours.is_open("щось_нове", kyiv(2026, 10, 24))


def test_fx_weekly_window_follows_new_york_time():
    # П'ятниця 23:30 за Києвом = 16:30 у Нью-Йорку — ще торгується.
    assert market_hours.in_weekly_window(kyiv(2026, 10, 23, 23, 30))
    # Субота 00:30 за Києвом = п'ятниця 17:30 у Нью-Йорку — вже закрито.
    assert not market_hours.in_weekly_window(kyiv(2026, 10, 24, 0, 30))
    # Неділя 18.10 23:30 за Києвом = 16:30 у Нью-Йорку — ще закрито.
    assert not market_hours.in_weekly_window(kyiv(2026, 10, 18, 23, 30))
    # Понеділок 00:30 за Києвом = неділя 17:30 у Нью-Йорку — відкрито.
    assert market_hours.in_weekly_window(kyiv(2026, 10, 19, 0, 30))


def test_fx_window_survives_mismatched_dst_week():
    """25.10 Європа вже на зимовому часі, США — ще ні (до 1.11): різниця
    6 год, і неділя 23:30 за Києвом — це вже 17:30 у Нью-Йорку. Вікно
    рахується в поясі Нью-Йорка, тож зсув DST не ламає його."""
    assert market_hours.in_weekly_window(kyiv(2026, 10, 25, 23, 30))
    assert not market_hours.in_weekly_window(kyiv(2026, 10, 25, 22, 30))


def test_session_uses_exchange_local_date_and_holidays():
    """Японське свято (3.11, День культури) закриває лише азійську сесію."""
    culture_day = kyiv(2026, 11, 3, 2, 15)  # 09:15 у Токіо
    assert not market_hours.exchange_session_today("asia", culture_day)
    assert market_hours.exchange_session_today("europe", kyiv(2026, 11, 3, 8))
    # Азійський бриф о 02:15 за Києвом у суботу — субота в Токіо.
    assert not market_hours.exchange_session_today("asia", kyiv(2026, 10, 24, 2, 15))
    # ...а в понеділок о 02:15 — понеділок у Токіо.
    assert market_hours.exchange_session_today("asia", kyiv(2026, 10, 26, 2, 15))


def test_previous_day_traded_drives_morning_screening():
    """Скринінг о 05:00 бере закриття ВЧОРАШНЬОЇ сесії: субота — так
    (п'ятниця), неділя й понеділок — ні, ранок після свята — ні."""
    assert market_hours.previous_day_traded("stock", kyiv(2026, 10, 24, 5))
    assert not market_hours.previous_day_traded("stock", kyiv(2026, 10, 25, 5))
    assert not market_hours.previous_day_traded("stock", kyiv(2026, 10, 26, 5))
    assert market_hours.previous_day_traded("stock", kyiv(2026, 10, 27, 5))
    assert not market_hours.previous_day_traded("stock", kyiv(2026, 11, 27, 5))


def test_sessions_between_counts_only_trading_days():
    assert market_hours.sessions_between("stock", date(2026, 10, 23), date(2026, 10, 26)) == 1
    assert market_hours.sessions_between("stock", date(2026, 10, 24), date(2026, 10, 25)) == 0


def test_schedule_gate_reasons():
    saturday = kyiv(2026, 10, 24, 6)
    assert market_hours.schedule_gate({"markets": ("stock", "fx")}, saturday)
    assert market_hours.schedule_gate({"markets": ("stock", "crypto")}, saturday) is None
    assert "XJPX" in market_hours.schedule_gate({"session": "asia"}, kyiv(2026, 10, 24, 2, 15))
    assert market_hours.schedule_gate({"after_session": "stock"}, kyiv(2026, 10, 24, 5)) is None
    assert market_hours.schedule_gate({"after_session": "stock"}, kyiv(2026, 10, 26, 5))
    # Без ключів календаря — запускати завжди.
    assert market_hours.schedule_gate({}, saturday) is None


def test_asset_is_open_treats_unknown_ids_as_stock_tickers():
    classes = {"btc": "crypto", "xauusd": "commodity"}
    saturday = kyiv(2026, 10, 24)
    assert market_hours.asset_is_open("btc", classes, saturday)
    assert not market_hours.asset_is_open("xauusd", classes, saturday)
    assert not market_hours.asset_is_open("META", classes, saturday)
    assert market_hours.asset_is_open("META", classes, kyiv(2026, 10, 26))
