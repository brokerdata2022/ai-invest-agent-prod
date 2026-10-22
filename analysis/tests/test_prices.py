from datetime import date, timedelta
from decimal import Decimal

import news_analysis.prices as prices_module
from news_analysis.prices import (
    ANOMALY_MIN_HISTORY,
    _resolve_price_source,
    compute_pct_change,
    fetch_latest_observed_at,
    fetch_price_change,
    fetch_price_history,
    is_anomalous_move,
)


def test_compute_pct_change_positive():
    change = compute_pct_change(
        "xauusd", Decimal("4000"), "2026-09-20", Decimal("4200"), "2026-09-26"
    )
    assert change.pct_change == Decimal("5")
    assert change.asset_id == "xauusd"


def test_compute_pct_change_negative():
    change = compute_pct_change(
        "wti_crude", Decimal("100"), "2026-09-20", Decimal("92"), "2026-09-26"
    )
    assert change.pct_change == Decimal("-8")


def test_compute_pct_change_zero_start_value_does_not_divide_by_zero():
    change = compute_pct_change("eurusd", Decimal("0"), "2026-09-20", Decimal("1"), "2026-09-26")
    assert change.pct_change == Decimal("0")


def test_compute_pct_change_no_movement():
    change = compute_pct_change(
        "brent_crude", Decimal("80"), "2026-09-20", Decimal("80"), "2026-09-26"
    )
    assert change.pct_change == Decimal("0")


# Живий список у тестах — те, що віддав би
# `common/watchlist_db.py:fetch_price_sources(conn)`. Хардкоду в модулі
# більше немає (2026-10-22), тож кожен тест передає джерела явно.
_LIVE = {
    "wti_crude": ("yahoo_chart", "wti_crude"),
    "brent_crude": ("yahoo_chart", "brent_crude"),
    "usdjpy": ("yahoo_chart", "usdjpy"),
    "gbpusd": ("twelvedata", "gbpusd_close"),
}


def test_prices_module_has_no_hardcoded_source_map():
    # 2026-10-22: `ASSET_PRICE_SOURCES` двічі тихо розходився з
    # таблицею (2026-10-02, 2026-10-13). Повернення хардкоду мусить
    # ламати тест, а не поведінку.
    assert not hasattr(prices_module, "ASSET_PRICE_SOURCES")


def test_resolve_price_source_uses_live_mapping_when_known():
    assert _resolve_price_source("usdjpy", _LIVE) == ("yahoo_chart", "usdjpy")


def test_resolve_price_source_falls_back_to_twelvedata_convention_for_ticker():
    # тикер зі скринінгу (docs/decisions.md, "Ціль 2") -- не в живому
    # списку, здогад за конвенцією twelvedata_adapter.py.
    assert _resolve_price_source("AAPL", _LIVE) == ("twelvedata", "aapl_close")


def test_resolve_price_source_fallback_lowercases_ticker():
    assert _resolve_price_source("NVDA", {}) == ("twelvedata", "nvda_close")


def test_resolve_price_source_uses_telegram_added_asset():
    assert _resolve_price_source("gbpusd", _LIVE) == ("twelvedata", "gbpusd_close")


# --- Регресія 2026-09-29: fetch_price_change() поверне None однаково і
# коли даних НІКОЛИ не було, і коли вони є, але застаріли поза вікном
# --days — synthesize.py тепер викликає fetch_latest_observed_at() ОКРЕМО,
# щоб чесно розрізнити ці два випадки в лозі, замість одного нейтрального
# "пропущено" для обох.


class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self.executed = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params):
        self.executed = (query, params)

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self.cursor_obj = _FakeCursor(row)

    def cursor(self):
        return self.cursor_obj


def test_fetch_latest_observed_at_returns_none_when_never_collected():
    conn = _FakeConn((None,))
    assert fetch_latest_observed_at(conn, "wti_crude", price_sources=_LIVE) is None


def test_fetch_latest_observed_at_returns_date_ignoring_window():
    conn = _FakeConn((date(2026, 9, 22),))
    assert fetch_latest_observed_at(conn, "wti_crude", price_sources=_LIVE) == date(2026, 9, 22)


def test_fetch_latest_observed_at_uses_resolved_source_and_no_days_filter():
    conn = _FakeConn((None,))
    fetch_latest_observed_at(conn, "wti_crude", price_sources=_LIVE)
    query, params = conn.cursor_obj.executed
    assert "days" not in query  # на відміну від fetch_price_change — вікно тут навмисно не застосовується
    assert params == ("yahoo_chart", "wti_crude")


def test_fetch_latest_observed_at_honors_explicit_price_sources():
    conn = _FakeConn((None,))
    fetch_latest_observed_at(conn, "gbpusd", price_sources={"gbpusd": ("twelvedata", "gbpusd_close")})
    _, params = conn.cursor_obj.executed
    assert params == ("twelvedata", "gbpusd_close")


# --- 2026-10-04, живий фідбек користувача: synthesize.py видав "Brent
# -5.01%" з пари точок 6-денної давнини -- технічно "в межах" 7-денного
# вікна `days`, але вже не поточний стан ринку. fetch_price_change()
# тепер додатково вимагає, щоб НАЙНОВІША точка сама не була застарілою
# (common/freshness.py:is_stale(), БУДНІ дні відносно date.today()).


class _FakeRowsCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params):
        self.executed = (query, params)

    def fetchall(self):
        return self._rows


class _FakeRowsConn:
    def __init__(self, rows):
        self.cursor_obj = _FakeRowsCursor(rows)

    def cursor(self):
        return self.cursor_obj


def test_fetch_price_change_returns_none_when_latest_point_is_stale():
    # 10 календарних днів -- мінімум 6 будніх днів за будь-якого
    # вирівнювання тижня, гарантовано понад STALE_THRESHOLD_BUSINESS_DAYS
    # незалежно від того, яким днем тижня є date.today() під час прогону.
    stale_end = date.today() - timedelta(days=10)
    stale_start = stale_end - timedelta(days=1)
    conn = _FakeRowsConn([(stale_start, "120.00"), (stale_end, "113.96")])
    assert fetch_price_change(conn, "brent_crude", price_sources=_LIVE) is None


def test_fetch_price_change_returns_result_when_latest_point_is_fresh():
    # observed_at = сьогодні -- 0 будніх днів розриву за будь-якого дня
    # тижня, трівіально не застаріло.
    fresh_end = date.today()
    fresh_start = fresh_end - timedelta(days=1)
    conn = _FakeRowsConn([(fresh_start, "100.00"), (fresh_end, "105.00")])
    change = fetch_price_change(conn, "brent_crude", price_sources=_LIVE)
    assert change is not None
    assert change.pct_change == Decimal("5")


# --- 2026-10-02, живий фідбек користувача: синтез не має залежати
# ЛИШЕ від новин — рух ціни сам по собі (без жодної новини) теж привід
# проаналізувати актив, якщо він НЕЗВИЧНИЙ для власної історії активу.


def test_fetch_price_history_returns_chronological_order(monkeypatch):
    # fetch_recent() (common/db.py) повертає найновіше ПЕРШИМ --
    # fetch_price_history() має розвернути в хронологічний порядок.
    newest_first = [{"value": "103"}, {"value": "102"}, {"value": "101"}]
    monkeypatch.setattr(
        prices_module, "fetch_recent",
        lambda conn, source, metric_id, limit: newest_first,
    )
    history = fetch_price_history(conn=None, asset_id="wti_crude", limit=10, price_sources=_LIVE)
    assert history == [Decimal("101"), Decimal("102"), Decimal("103")]


def _history_from_returns(start: float, returns_pct: list[float]) -> list[Decimal]:
    """Хронологічний ряд значень, де КОЖЕН крок -- точно заданий %
    денного доходу -- для детермінованого контролю stdev у тестах
    is_anomalous_move()."""
    values = [Decimal(str(start))]
    for r in returns_pct:
        values.append(values[-1] * (Decimal("1") + Decimal(str(r)) / Decimal("100")))
    return values


# ~1% денна волатильність (чергування +1/-1), ANOMALY_MIN_HISTORY+1 точок.
_STABLE_RETURNS = [1, -1, 1, -1, 1, -1, 1, -1, 1, -1, 1][: ANOMALY_MIN_HISTORY + 1]


def test_is_anomalous_move_true_for_move_far_outside_normal_volatility():
    history = _history_from_returns(100, _STABLE_RETURNS)
    assert is_anomalous_move(Decimal("15"), history, window_days=7) is True


def test_is_anomalous_move_false_for_move_within_normal_volatility():
    history = _history_from_returns(100, _STABLE_RETURNS)
    assert is_anomalous_move(Decimal("1"), history, window_days=7) is False


def test_is_anomalous_move_false_when_not_enough_history():
    # Менше за ANOMALY_MIN_HISTORY денних доходностей -- немає
    # статистичної опори судити, навіть на дуже великий рух.
    history = _history_from_returns(100, [1, -1, 1])
    assert is_anomalous_move(Decimal("50"), history, window_days=7) is False


def test_is_anomalous_move_false_when_history_is_flat():
    # Нульова волатильність (stdev=0) -- діагностично "немає з чим
    # порівнювати", не хибне "усе аномальне".
    history = [Decimal("100")] * (ANOMALY_MIN_HISTORY + 2)
    assert is_anomalous_move(Decimal("20"), history, window_days=7) is False


def test_fetch_price_history_respects_live_price_sources(monkeypatch):
    """ЛАТЕНТНИЙ БАГ, знайдений 2026-10-13. Функція викликала
    `_resolve_price_source(asset_id)` БЕЗ живого списку, тож завжди
    падала на хардкод `ASSET_PRICE_SOURCES`. Поки хардкод і
    `watchlist_assets` збігались, це було непомітно. Але 2026-10-12
    watchlist перевели на `yahoo_chart` (127-251 точка), а хардкод
    лишився на `tradingeconomics` з 6-7 точками — тобто детектор
    аномалій читав саме той ряд, де `ANOMALY_MIN_HISTORY`=10 ніколи не
    виконується, і повертав False ЗАВЖДИ. Не падав, не логував —
    просто тихо нічого не знаходив."""
    from news_analysis import prices as prices_module

    seen = {}

    def fake_fetch_recent(conn, source, metric_id, limit):
        seen["source"] = source
        seen["metric_id"] = metric_id
        return [{"value": Decimal("1")}]

    monkeypatch.setattr(prices_module, "fetch_recent", fake_fetch_recent)
    prices_module.fetch_price_history(
        conn=None, asset_id="wti_crude",
        price_sources={"wti_crude": ("yahoo_chart", "wti_crude")},
    )

    assert seen["source"] == "yahoo_chart", (
        "джерело мусить братись із живого списку"
    )


def test_fetch_price_history_requires_live_price_sources():
    """Без живого списку виклик тепер ПАДАЄ, а не тихо читає хардкод
    (2026-10-22): саме тиха підміна джерела й була дефектом 2026-10-13."""
    import pytest

    with pytest.raises(TypeError):
        fetch_price_history(conn=None, asset_id="wti_crude")
