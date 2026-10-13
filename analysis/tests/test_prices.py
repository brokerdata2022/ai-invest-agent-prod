from datetime import date, timedelta
from decimal import Decimal

import news_analysis.prices as prices_module
from news_analysis.prices import (
    ANOMALY_MIN_HISTORY,
    ASSET_PRICE_SOURCES,
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


def test_asset_price_sources_known_watchlist_assets():
    # 2026-10-04: xauusd перемкнуто з twelvedata на tradingeconomics --
    # НЕ через застарілість, а через нестабільні заднім числом ревізії
    # Twelve Data forex-OTC котирування (той самий 2026-09-28 отримав
    # 2 ревізії з різницею 81 пункт).
    assert ASSET_PRICE_SOURCES["xauusd"] == ("tradingeconomics", "xauusd")
    # 2026-10-04 (critical rule 7, CLAUDE.md): coffee/wti_crude/
    # brent_crude/natgas -- усі чотири товарні FRED-серії застрягли чи
    # мали принципово недостатню частоту -- перемкнуто на
    # tradingeconomics (commodities/tradingeconomics_adapter.py,
    # справжній автоматизований скрапінг на розкладі, не ручний
    # крос-чек) -- "один працюючий код", не разовий фікс на кожен актив.
    assert ASSET_PRICE_SOURCES["coffee"] == ("tradingeconomics", "coffee")
    assert ASSET_PRICE_SOURCES["wti_crude"] == ("tradingeconomics", "wti_crude")
    assert ASSET_PRICE_SOURCES["brent_crude"] == ("tradingeconomics", "brent_crude")
    assert ASSET_PRICE_SOURCES["natgas"] == ("tradingeconomics", "natgas")


def test_asset_price_sources_eurusd_usdjpy_use_twelvedata_not_fred():
    # 2026-10-02: перейшли з FRED (DEXUSEU/DEXJPUS) на Twelve Data --
    # живо підтверджено на fred.stlouisfed.org, що обидві FRED-серії
    # застрягли на 2026-09-25 (Next Release Date: 2026-10-05) попри
    # заявлену щоденну частоту -- не наш баг збору, затримка публікації
    # на боці джерела. Стара fred-серія "usdjpy_fx_rate" лишається в
    # raw_observations назавжди (rule 6 CLAUDE.md), просто вже не
    # поточне джерело для цих asset_id.
    assert ASSET_PRICE_SOURCES["eurusd"] == ("twelvedata", "eurusd_close")
    assert ASSET_PRICE_SOURCES["usdjpy"] == ("twelvedata", "usdjpy_close")


def test_asset_price_sources_crypto_uses_binance_not_twelvedata_fallback():
    # 2026-10-02: без явного мапінгу тут _resolve_price_source() гадав
    # twelvedata-конвенцію для btc/eth/sol, де даних для крипти ніколи
    # не було -- fetch_price_change() завжди повертав None.
    assert ASSET_PRICE_SOURCES["btc"] == ("binance", "btc_close")
    assert ASSET_PRICE_SOURCES["eth"] == ("binance", "eth_close")
    assert ASSET_PRICE_SOURCES["sol"] == ("binance", "sol_close")


def test_asset_price_sources_xagusd_uses_coingecko_not_twelvedata():
    # xagusd заблоковано тарифом ЛИШЕ на Twelve Data (docs/decisions.md
    # 2026-09-26) -- ціна вже йде через CoinGecko (kinesis-silver
    # токен-проксі), fallback на twelvedata-конвенцію тут був хибним.
    assert ASSET_PRICE_SOURCES["xagusd"] == ("coingecko", "xagusd_close")


def test_resolve_price_source_uses_explicit_mapping_when_known():
    assert _resolve_price_source("usdjpy") == ("twelvedata", "usdjpy_close")


def test_resolve_price_source_falls_back_to_twelvedata_convention_for_ticker():
    # тикер зі скринінгу (docs/decisions.md, "Ціль 2") -- не в
    # ASSET_PRICE_SOURCES, здогад за конвенцією twelvedata_adapter.py.
    assert _resolve_price_source("AAPL") == ("twelvedata", "aapl_close")


def test_resolve_price_source_fallback_lowercases_ticker():
    assert _resolve_price_source("NVDA") == ("twelvedata", "nvda_close")


def test_resolve_price_source_uses_explicit_price_sources_when_given():
    # 2026-10-03: живий (редагований через Telegram) список —
    # common/watchlist_db.py:fetch_price_sources(conn) — перекриває
    # статичний ASSET_PRICE_SOURCES, коли переданий явно.
    live_sources = {"gbpusd": ("twelvedata", "gbpusd_close")}
    assert _resolve_price_source("gbpusd", live_sources) == ("twelvedata", "gbpusd_close")


def test_resolve_price_source_explicit_price_sources_still_falls_back_for_unknown():
    live_sources = {"gbpusd": ("twelvedata", "gbpusd_close")}
    assert _resolve_price_source("AAPL", live_sources) == ("twelvedata", "aapl_close")


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
    assert fetch_latest_observed_at(conn, "wti_crude") is None


def test_fetch_latest_observed_at_returns_date_ignoring_window():
    conn = _FakeConn((date(2026, 9, 22),))
    assert fetch_latest_observed_at(conn, "wti_crude") == date(2026, 9, 22)


def test_fetch_latest_observed_at_uses_resolved_source_and_no_days_filter():
    conn = _FakeConn((None,))
    fetch_latest_observed_at(conn, "wti_crude")
    query, params = conn.cursor_obj.executed
    assert "days" not in query  # на відміну від fetch_price_change — вікно тут навмисно не застосовується
    assert params == ("tradingeconomics", "wti_crude")  # 2026-10-04: wti_crude перемкнуто на tradingeconomics


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
    assert fetch_price_change(conn, "brent_crude") is None


def test_fetch_price_change_returns_result_when_latest_point_is_fresh():
    # observed_at = сьогодні -- 0 будніх днів розриву за будь-якого дня
    # тижня, трівіально не застаріло.
    fresh_end = date.today()
    fresh_start = fresh_end - timedelta(days=1)
    conn = _FakeRowsConn([(fresh_start, "100.00"), (fresh_end, "105.00")])
    change = fetch_price_change(conn, "brent_crude")
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
    history = fetch_price_history(conn=None, asset_id="wti_crude", limit=10)
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
        "живий список джерел мусить перекривати хардкод ASSET_PRICE_SOURCES"
    )


def test_fetch_price_history_still_falls_back_to_hardcoded_map(monkeypatch):
    """Без живого списку поведінка лишається попередньою — щоб
    викликачі, які його не мають, не зламались."""
    from news_analysis import prices as prices_module

    seen = {}

    def fake_fetch_recent(conn, source, metric_id, limit):
        seen["source"] = source
        return [{"value": Decimal("1")}]

    monkeypatch.setattr(prices_module, "fetch_recent", fake_fetch_recent)
    prices_module.fetch_price_history(conn=None, asset_id="wti_crude")
    assert seen["source"] == prices_module.ASSET_PRICE_SOURCES["wti_crude"][0]
