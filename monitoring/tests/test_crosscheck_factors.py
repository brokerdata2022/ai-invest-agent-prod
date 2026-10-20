"""
Звірка факторних рядів (`monitoring/crosscheck_factors.py`) — чиста
логіка без БД, на числах живого заміру 2026-10-20.
"""

from datetime import date
from decimal import Decimal

import config
from crosscheck_factors import compare_series, stale_series

D = Decimal


def test_diff_pair_compares_weekly_change_not_level():
    """TradingEconomics публікує ЗМІНУ запасів, EIA — рівень. Живо:
    415 577 − 424 134 = −8 557 проти TE −8 560 — згода; а рівень
    415 577 у медіану не потрапляє (інші одиниці)."""
    spec = config.FACTOR_SERIES["us_crude_stocks"]
    primary = {date(2026, 10, 2): D("424134"), date(2026, 10, 9): D("415577")}
    te = {date(2026, 10, 9): D("-8560")}
    pair = {"source": "tradingeconomics", "metric_id": "us_crude_stocks_change", "compare": "diff"}

    day, verdict, values = compare_series("us_crude_stocks", spec, primary, [(pair, te)])

    assert day == date(2026, 10, 9)
    assert verdict.status == "agree"
    assert values == {"eia": D("-8557"), "tradingeconomics": D("-8560")}
    assert verdict.consensus == D("415577"), "в аналіз іде рівень, не зміна"


def test_diff_pair_catches_wrong_level():
    """Зіпсований рівень EIA дав би іншу зміну — звірка це бачить."""
    spec = config.FACTOR_SERIES["us_crude_stocks"]
    primary = {date(2026, 10, 2): D("424134"), date(2026, 10, 9): D("405577")}
    te = {date(2026, 10, 9): D("-8560")}
    pair = {"source": "tradingeconomics", "metric_id": "us_crude_stocks_change", "compare": "diff"}

    _, verdict, _ = compare_series("us_crude_stocks", spec, primary, [(pair, te)])
    assert verdict.status == "disagree"


def test_level_pair_uses_latest_common_date():
    """blockchain.info відстає на ~6 днів: звіряється остання СПІЛЬНА
    дата, а не останні точки кожного (різні дні дали б хибну
    розбіжність — урок crosscheck_macro)."""
    spec = config.FACTOR_SERIES["btc_tx_count"]
    primary = {date(2026, 10, 13): D("841198"), date(2026, 10, 14): D("789873"), date(2026, 10, 19): D("663684")}
    bc = {date(2026, 10, 13): D("838363"), date(2026, 10, 14): D("803842")}
    pair = {"source": "blockchain_info", "metric_id": "btc_tx_count", "compare": "level"}

    day, verdict, _ = compare_series("btc_tx_count", spec, primary, [(pair, bc)])
    assert day == date(2026, 10, 14)
    assert verdict.status == "agree"   # 1.77% — найгірший живий розкид, у межах 3%


def test_single_source_is_named_not_hidden():
    spec = config.FACTOR_SERIES["btc_etf_flow_usd_m"]
    primary = {date(2026, 10, 16): D("-82.4"), date(2026, 10, 19): D("526.7")}

    day, verdict, values = compare_series("btc_etf_flow_usd_m", spec, primary, [])
    assert day == date(2026, 10, 19)
    assert verdict.status == "insufficient"
    assert "одне джерело" in verdict.detail
    assert values == {"farside": D("526.7")}


def test_no_common_date_is_insufficient_not_agree():
    """Друге джерело не зібралось — це НЕ "згода"."""
    spec = config.FACTOR_SERIES["eth_tx_count"]
    primary = {date(2026, 10, 19): D("1748461")}
    pair = {"source": "etherscan", "metric_id": "eth_tx_count", "compare": "level"}

    _, verdict, _ = compare_series("eth_tx_count", spec, primary, [(pair, {})])
    assert verdict.status == "insufficient"
    assert "немає спільної дати" in verdict.detail


def test_empty_primary_returns_none():
    assert compare_series("gld_tonnes", config.FACTOR_SERIES["gld_tonnes"], {}, []) is None


def test_weather_models_spread_within_hdd_tolerance():
    """Живий замір 2026-10-20: 53.5 проти 82.8 HDD — розкид моделей
    (фронт на 7-й день), не помилка; у межах абсолютного допуску."""
    spec = config.FACTOR_SERIES["us_hdd_7d"]
    pair = {"source": "met_norway", "metric_id": "us_hdd_7d", "compare": "level"}
    _, verdict, _ = compare_series(
        "us_hdd_7d", spec, {date(2026, 10, 20): D("53.5")}, [(pair, {date(2026, 10, 20): D("82.8")})],
    )
    assert verdict.status == "agree"


def test_stale_series_by_business_days():
    """Тижневий EIA: 8 будніх днів до наступного релізу — норма; ETF
    (поріг 2) після тижня тиші — застарів; ряду без жодної точки — теж."""
    today = date(2026, 10, 21)
    stale = stale_series({
        "us_crude_stocks": date(2026, 10, 9),
        "btc_etf_flow_usd_m": date(2026, 10, 14),
        "gld_tonnes": None,
    }, today=today)
    assert stale == ["btc_etf_flow_usd_m", "gld_tonnes"]


def test_saudi_production_two_estimates_agree_within_method_gap():
    """Живо 2026-10-20: EIA 8.10 проти TradingEconomics 8.372 млн
    бар./добу — дві оцінки різними методиками; розрив 3.4% у межах."""
    spec = config.FACTOR_SERIES["saudi_crude_production"]
    pair = {"source": "tradingeconomics", "metric_id": "saudi_crude_production", "compare": "level"}
    _, verdict, _ = compare_series(
        "saudi_crude_production", spec,
        {date(2026, 9, 1): D("8.1")}, [(pair, {date(2026, 9, 1): D("8.372")})],
    )
    assert verdict.status == "agree"


def test_saudi_unit_error_is_caught():
    """Якби TE не перевели з тис. у млн — 8372 проти 8.1, тривога."""
    spec = config.FACTOR_SERIES["saudi_crude_production"]
    pair = {"source": "tradingeconomics", "metric_id": "saudi_crude_production", "compare": "level"}
    _, verdict, _ = compare_series(
        "saudi_crude_production", spec,
        {date(2026, 9, 1): D("8.1")}, [(pair, {date(2026, 9, 1): D("8372")})],
    )
    assert verdict.status == "disagree"


def test_event_series_never_stale_but_missing_is():
    """Інтервенції Японії: остання — травень, і це норма, а не
    застарілість. Але ряд без жодної точки — збій збору."""
    today = date(2026, 10, 21)
    assert stale_series({"japan_fx_intervention": date(2026, 5, 6)}, today=today) == []
    assert stale_series({"japan_fx_intervention": None}, today=today) == ["japan_fx_intervention"]
