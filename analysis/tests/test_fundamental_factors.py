"""
Тести збирання чинників по активу (`analysis/fundamental/factors.py`)
і промпта за чинниками. Фейковий курсор замість БД — той самий підхід,
що test_watchlist_db.py.

Окрема увага — живому дефекту 2026-10-04: у промпт пішов РІВЕНЬ
індексу CPI, і LLM вивів із цього "річну інфляцію ~1.1%" із
5-місячної зміни, побудувавши хибну реальну ставку. Звідси `yoy:` —
темп рахує КОД.
"""

from datetime import date
from decimal import Decimal

import config
import pytest

from fundamental.factors import AssetFactors, Factor, collect_asset_factors
from fundamental.fundamental_llm import build_asset_prompt


class _Cursor:
    """Віддає заготовлені рядки по черзі запитів."""

    def __init__(self, queue):
        self.queue = list(queue)
        self.description = [("observed_at",), ("value",)]

    def execute(self, query, params=None):
        self._rows = self.queue.pop(0) if self.queue else []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, queue):
        self._cursor = _Cursor(queue)

    def cursor(self):
        return self._cursor


# --- yoy: темп рахує код, не LLM -------------------------------------


def test_yoy_computed_from_year_apart_points():
    """Головний фікс: річна зміна — результат обчислення, не здогадки
    моделі з рівня індексу."""
    rows = [
        (date(2026, 8, 1), Decimal("334.131")),
        (date(2026, 3, 1), Decimal("330.293")),
        (date(2025, 8, 1), Decimal("323.300")),
    ]
    conn = _Conn([rows])
    from fundamental.factors import _fetch_yoy

    points = _fetch_yoy(conn, "fred", "cpi")
    assert len(points) == 1
    assert points[0]["value"] == "+3.35%"
    assert "2025-08-01" in points[0]["observed_at"]


def test_yoy_picks_closest_point_to_one_year_back():
    """Місячні серії рідко мають рівно ту саму дату — беремо найближчу
    в межах допуску, а не першу-ліпшу."""
    rows = [
        (date(2026, 8, 1), Decimal("110")),
        (date(2025, 8, 10), Decimal("100")),   # 9 днів від цілі — ближче
        (date(2025, 7, 20), Decimal("90")),    # 12 днів — далі
    ]
    from fundamental.factors import _fetch_yoy

    points = _fetch_yoy(_Conn([rows]), "fred", "cpi")
    assert points[0]["value"] == "+10.00%"


def test_yoy_empty_when_no_point_near_one_year():
    """Честно порожньо, а не підміна іншим періодом — саме підміна
    періоду й була коренем дефекту."""
    rows = [
        (date(2026, 8, 1), Decimal("110")),
        (date(2026, 3, 1), Decimal("100")),
    ]
    from fundamental.factors import _fetch_yoy

    assert _fetch_yoy(_Conn([rows]), "fred", "cpi") == []


def test_yoy_empty_on_no_data():
    from fundamental.factors import _fetch_yoy

    assert _fetch_yoy(_Conn([[]]), "fred", "cpi") == []


def test_yoy_handles_negative_base():
    rows = [
        (date(2026, 8, 1), Decimal("-5")),
        (date(2025, 8, 1), Decimal("-10")),
    ]
    from fundamental.factors import _fetch_yoy

    assert _fetch_yoy(_Conn([rows]), "fred", "cpi")[0]["value"] == "+50.00%"


# --- чек-листи в конфігу ---------------------------------------------


def test_every_watchlist_asset_has_factors():
    """Актив без чинників не аналізується взагалі — тож перелік мусить
    покривати весь watchlist (db/schema.sql seed)."""
    seeded = {
        "xauusd", "xagusd", "wti_crude", "brent_crude", "natgas",
        "coffee", "eurusd", "usdjpy", "btc", "eth", "sol",
    }
    assert seeded <= set(config.ASSET_FACTORS)


def test_factor_sets_differ_between_asset_types():
    """Суть рішення користувача: аналіз золота за показниками нафти
    безглуздий, тож набори мусять РІЗНИТИСЬ."""
    gold = {k for k, _ in config.ASSET_FACTORS["xauusd"]}
    oil = {k for k, _ in config.ASSET_FACTORS["wti_crude"]}
    fx = {k for k, _ in config.ASSET_FACTORS["usdjpy"]}
    assert gold != oil
    assert gold != fx
    assert "boj:japan_policy_rate" in fx, "carry trade — головний чинник USD/JPY"


def test_every_asset_includes_own_price():
    for asset_id, specs in config.ASSET_FACTORS.items():
        assert "price" in {k for k, _ in specs}, f"{asset_id}: немає власної ціни"


def test_inflation_factors_use_yoy_not_index_level():
    """Пряма перевірка фікса: там, де чинник за змістом є ТЕМПОМ, у
    чек-листі мусить стояти `yoy:`, не рівень індексу."""
    for asset_id in ("xauusd", "xagusd", "eurusd", "usdjpy"):
        keys = {k for k, _ in config.ASSET_FACTORS[asset_id]}
        assert "fred:cpi" not in keys, (
            f"{asset_id}: рівень індексу CPI замість річної зміни — саме це "
            f"дало хибну реальну ставку 2026-10-04"
        )


def test_gaps_declared_for_assets_with_known_blind_spots():
    for asset_id in ("wti_crude", "natgas", "coffee", "xauusd"):
        assert config.ASSET_FACTOR_GAPS.get(asset_id), (
            f"{asset_id}: прогалини в даних не названі — LLM домислюватиме"
        )


# --- промпт ----------------------------------------------------------


def _factors(**kw) -> AssetFactors:
    defaults = dict(
        asset_id="xauusd", label="Золото (XAU/USD)",
        factors=[
            Factor("price", "Ціна золота", points=[{"observed_at": "2026-10-02", "value": 4140}]),
            Factor("news", "Новинний фон", texts=["[3 джерел] прогноз знижено"]),
            Factor("fred:treasury_10y", "Дохідність 10Y", missing_reason="немає даних"),
        ],
        gaps=("потоки в золоті ETF",),
    )
    defaults.update(kw)
    return AssetFactors(**defaults)


def test_prompt_lists_available_factors():
    prompt = build_asset_prompt(_factors())
    assert "Ціна золота" in prompt
    assert "Новинний фон" in prompt
    assert "прогноз знижено" in prompt


def test_prompt_states_unavailable_factors_explicitly():
    """Чинник без даних перелічується ЯВНО: інакше LLM не знає, чи він
    неважливий, чи просто не дістався — і домислює."""
    prompt = build_asset_prompt(_factors())
    assert "ЧИННИКИ БЕЗ ДАНИХ" in prompt
    assert "Дохідність 10Y — немає даних" in prompt


def test_prompt_names_gaps_we_never_collect():
    prompt = build_asset_prompt(_factors())
    assert "НЕ МАЄМО ВЗАГАЛІ" in prompt
    assert "потоки в золоті ETF" in prompt


def test_prompt_omits_gap_block_when_none():
    prompt = build_asset_prompt(_factors(gaps=()))
    assert "НЕ МАЄМО ВЗАГАЛІ" not in prompt


def test_prompt_omits_unavailable_block_when_all_present():
    all_present = _factors(factors=[
        Factor("price", "Ціна золота", points=[{"observed_at": "2026-10-02", "value": 4140}]),
    ])
    assert "ЧИННИКИ БЕЗ ДАНИХ" not in build_asset_prompt(all_present)


def test_prompt_includes_asset_identity():
    prompt = build_asset_prompt(_factors())
    assert "Золото (XAU/USD)" in prompt
    assert "xauusd" in prompt


def test_collect_returns_missing_reason_for_unknown_factor_type():
    """Невідомий тип чинника не валить прогін — лишається з причиною."""
    original = config.ASSET_FACTORS.get("__test__")
    config.ASSET_FACTORS["__test__"] = (("хибний_ключ", "Щось"),)
    try:
        result = collect_asset_factors(
            _Conn([]), "__test__", "Тест", "fred", "x"
        )
        assert result.factors[0].missing_reason is not None
        assert "невідомий тип" in result.factors[0].missing_reason
    finally:
        if original is None:
            del config.ASSET_FACTORS["__test__"]
