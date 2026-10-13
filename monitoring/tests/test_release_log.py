"""
Тести чистої логіки release_log — без реальної БД (той самий принцип,
що common/db.py:decide_revision).
"""

from datetime import date, datetime, timezone

from release_log import is_past_buffer, should_seed_new_cycle


def test_no_existing_row_should_seed():
    assert should_seed_new_cycle(existing_scheduled_at=None, due_date=date(2026, 10, 14)) is True


def test_same_cycle_already_seeded_should_not_reseed():
    existing = datetime(2026, 10, 14, 8, 30, tzinfo=timezone.utc)
    assert should_seed_new_cycle(existing_scheduled_at=existing, due_date=date(2026, 10, 14)) is False


def test_same_date_different_time_still_same_cycle():
    # Уточнення точного часу (fallback 8:30 → реальний час з
    # economic_calendar) на пізнішому прогоні — той самий due_date,
    # не повинно плодити дублікат (ідентичність циклу — дата з FRED).
    existing = datetime(2026, 10, 14, 8, 30, tzinfo=timezone.utc)
    assert should_seed_new_cycle(existing_scheduled_at=existing, due_date=date(2026, 10, 14)) is False


def test_new_cycle_date_should_reseed():
    existing = datetime(2026, 9, 11, 8, 30, tzinfo=timezone.utc)
    assert should_seed_new_cycle(existing_scheduled_at=existing, due_date=date(2026, 10, 14)) is True


def test_is_past_buffer_before_scheduled_time():
    scheduled_at = datetime(2026, 9, 24, 8, 30, tzinfo=timezone.utc)
    now = datetime(2026, 9, 24, 8, 45, tzinfo=timezone.utc)
    assert is_past_buffer(scheduled_at, now, buffer_minutes=30) is False


def test_is_past_buffer_exactly_at_buffer_edge():
    scheduled_at = datetime(2026, 9, 24, 8, 30, tzinfo=timezone.utc)
    now = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)
    assert is_past_buffer(scheduled_at, now, buffer_minutes=30) is True


def test_is_past_buffer_well_after():
    scheduled_at = datetime(2026, 9, 24, 8, 30, tzinfo=timezone.utc)
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    assert is_past_buffer(scheduled_at, now, buffer_minutes=30) is True


def test_is_past_buffer_uses_default_buffer():
    """Дефолт — 0 (2026-10-02): check_releases тепер крутиться щохвилини
    (orchestration/schedule.py), тому пробуємо РІВНО в scheduled_at, без
    навмисної затримки перед першою спробою (docs/decisions.md)."""
    scheduled_at = datetime(2026, 9, 24, 8, 30, tzinfo=timezone.utc)
    just_before = datetime(2026, 9, 24, 8, 29, 59, tzinfo=timezone.utc)
    at_scheduled_time = datetime(2026, 9, 24, 8, 30, tzinfo=timezone.utc)
    assert is_past_buffer(scheduled_at, just_before) is False
    assert is_past_buffer(scheduled_at, at_scheduled_time) is True
