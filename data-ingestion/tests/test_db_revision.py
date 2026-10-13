"""
Тести чистої логіки визначення ревізії (common.db.decide_revision).
Без підключення до реальної БД — саме для цього логіка винесена
окремо від SQL-коду.
"""

from decimal import Decimal

from common.db import decide_revision


def test_first_observation_gets_revision_one():
    assert decide_revision(latest=None, new_value=Decimal("314.121")) == 1


def test_unchanged_value_is_skipped():
    latest = {"value": Decimal("314.121"), "revision": 1}
    assert decide_revision(latest, new_value=Decimal("314.121")) is None


def test_unchanged_value_skipped_even_with_string_stored_value():
    # psycopg2 з RealDictCursor може повернути NUMERIC як Decimal вже,
    # але на випадок драйверних відмінностей перевіряємо і рядок.
    latest = {"value": "314.121", "revision": 1}
    assert decide_revision(latest, new_value=Decimal("314.121")) is None


def test_changed_value_increments_revision():
    latest = {"value": Decimal("314.121"), "revision": 1}
    assert decide_revision(latest, new_value=Decimal("314.200")) == 2


def test_revision_increments_from_current_max_not_always_from_one():
    latest = {"value": Decimal("100.0"), "revision": 4}
    assert decide_revision(latest, new_value=Decimal("100.5")) == 5
