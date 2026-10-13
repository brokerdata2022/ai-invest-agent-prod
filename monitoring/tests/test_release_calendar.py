"""
Тести release_calendar на збереженому прикладі відповіді (JSON, реальна
відповідь `GET /fred/release/dates?release_id=10&...`, отримана живим
запитом 2026-09-26 — CPI, release_id 10) — жодних реальних мережевих
викликів. Фікстура містить і минулі, і вже заплановані майбутні дати
(2026-10-14/11-10/12-10) — офіційний графік публікацій FRED.
"""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from release_calendar import (
    RELEASE_IDS,
    default_scheduled_datetime,
    next_scheduled_release,
    parse_release_dates,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def cpi_release_dates_response():
    with open(FIXTURES_DIR / "fred_release_dates_cpi_response.json", encoding="utf-8") as f:
        return json.load(f)


def test_release_ids_cover_nfp_and_unemployment_same_release():
    # Live-факт (2026-09-26): PAYEMS і UNRATE — той самий release_id 50
    # ("Employment Situation", той самий звіт BLS).
    assert RELEASE_IDS["nonfarm_payrolls"] == RELEASE_IDS["unemployment_rate"] == 50


def test_parse_release_dates_sorted_ascending(cpi_release_dates_response):
    dates = parse_release_dates(cpi_release_dates_response)
    assert dates == sorted(dates)
    assert date(2026, 9, 11) in dates
    assert date(2026, 12, 10) in dates  # майбутня запланована дата


def test_parse_release_dates_handles_missing_key():
    assert parse_release_dates({"unexpected": "shape"}) == []


def test_next_scheduled_release_finds_nearest_upcoming_date_within_wide_horizon(cpi_release_dates_response):
    dates = parse_release_dates(cpi_release_dates_response)
    # "Сьогодні" зафіксовано на 2026-09-26 (той самий момент живого
    # запиту) — найближча ще не минула дата — 2026-10-14 (наступний
    # CPI-реліз), не 2026-09-11 (той уже позаду). Широкий горизонт
    # (30 днів), щоб ізолювати саме цю поведінку від горизонту.
    result = next_scheduled_release(dates, today=date(2026, 9, 26), horizon_days=30)
    assert result == date(2026, 10, 14)


def test_next_scheduled_release_default_horizon_covers_monthly_release(cpi_release_dates_response):
    # Переписано 2026-10-11 разом зі зміною горизонту 7 → 21
    # (config.CALENDAR_HORIZON_DAYS). Раніше цей тест закріплював
    # протилежне: CPI-реліз за 18 днів НЕ заводився, бо не вліз у
    # 7-денне вікно. Живий вимір показав, чого це коштувало — за два
    # тижні роботи 9 показників із 15 жодного разу не потрапили в
    # release_log, зокрема cpi і core_cpi. Місячний реліз мусить
    # влазити в горизонт, інакше його ніколи не видно наперед.
    dates = parse_release_dates(cpi_release_dates_response)
    result = next_scheduled_release(dates, today=date(2026, 9, 26))
    assert result == date(2026, 10, 14)


def test_next_scheduled_release_default_horizon_still_excludes_far_future(cpi_release_dates_response):
    # Горизонт лишається РЕАЛЬНОЮ межею, не нескінченністю: від
    # 2026-09-11 наступна дата 2026-10-14 це 33 дні — за межами 21,
    # тож seed не йде (інакше `_seed_or_enrich` заводив би fallback-час
    # на місяці наперед, коли ForexFactory-фід ще нічого не знає).
    dates = parse_release_dates(cpi_release_dates_response)
    assert next_scheduled_release(dates, today=date(2026, 9, 12)) is None


def test_next_scheduled_release_includes_today():
    result = next_scheduled_release([date(2026, 9, 26), date(2026, 10, 14)], today=date(2026, 9, 26))
    assert result == date(2026, 9, 26)


def test_next_scheduled_release_respects_horizon_boundary():
    today = date(2026, 9, 26)
    assert next_scheduled_release([today + timedelta(days=7)], today=today, horizon_days=7) == today + timedelta(days=7)
    assert next_scheduled_release([today + timedelta(days=8)], today=today, horizon_days=7) is None


def test_next_scheduled_release_returns_none_when_all_past():
    result = next_scheduled_release([date(2026, 8, 1)], today=date(2026, 9, 26))
    assert result is None


def test_next_scheduled_release_returns_none_for_empty_list():
    assert next_scheduled_release([], today=date(2026, 9, 26)) is None


def test_default_scheduled_datetime_uses_830_eastern():
    dt = default_scheduled_datetime(date(2026, 9, 24))
    assert dt.hour == 8 and dt.minute == 30
    assert dt.tzinfo.key == "America/New_York"


def test_default_scheduled_datetime_handles_dst_transition():
    # EDT (літній час, UTC-4) проти EST (зимовий, UTC-5) — та сама
    # 8:30 місцевого часу, різний офсет від UTC.
    summer = default_scheduled_datetime(date(2026, 7, 1))
    winter = default_scheduled_datetime(date(2026, 12, 1))
    assert summer.utcoffset().total_seconds() / 3600 == -4
    assert winter.utcoffset().total_seconds() / 3600 == -5
