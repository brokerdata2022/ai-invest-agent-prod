from datetime import date

from common.freshness import business_days_between, is_stale


def test_business_days_between_same_day_is_zero():
    assert business_days_between(date(2026, 10, 5), date(2026, 10, 5)) == 0


def test_business_days_between_counts_only_weekdays():
    # П'ятниця (2) -> субота/неділя не рахуються, лише понеділок (1).
    friday = date(2026, 10, 2)
    monday = date(2026, 10, 5)
    assert business_days_between(friday, monday) == 1


def test_business_days_between_weekend_only_is_zero():
    friday = date(2026, 10, 2)
    sunday = date(2026, 10, 4)
    assert business_days_between(friday, sunday) == 0


def test_business_days_between_two_weekdays():
    monday = date(2026, 10, 5)
    wednesday = date(2026, 10, 7)
    assert business_days_between(monday, wednesday) == 2


def test_is_stale_false_for_friday_data_on_saturday():
    # Ринки закриті -- п'ятничні дані свіжі в суботу.
    assert is_stale(observed_at=date(2026, 10, 2), today=date(2026, 10, 3)) is False


def test_is_stale_false_for_friday_data_on_sunday():
    assert is_stale(observed_at=date(2026, 10, 2), today=date(2026, 10, 4)) is False


def test_is_stale_false_for_friday_data_on_monday():
    # Рівно 1 будній день лагу (понеділок) -- у межах порогу, НЕ застаріло.
    assert is_stale(observed_at=date(2026, 10, 2), today=date(2026, 10, 5)) is False


def test_is_stale_true_for_friday_data_on_tuesday():
    # 2 будні дні лагу (понеділок+вівторок) -- застаріло.
    assert is_stale(observed_at=date(2026, 10, 2), today=date(2026, 10, 6)) is True


def test_is_stale_true_for_two_weekday_gap():
    assert is_stale(observed_at=date(2026, 10, 5), today=date(2026, 10, 7)) is True


def test_is_stale_false_for_one_weekday_gap():
    assert is_stale(observed_at=date(2026, 10, 5), today=date(2026, 10, 6)) is False


def test_is_stale_honors_custom_threshold():
    assert is_stale(
        observed_at=date(2026, 10, 2), today=date(2026, 10, 6), threshold_business_days=3,
    ) is False
