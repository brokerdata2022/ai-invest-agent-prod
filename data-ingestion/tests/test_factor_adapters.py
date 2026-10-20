"""
Адаптери факторних рядів (2026-10-20): запаси EIA, тижнева зміна з
TradingEconomics, GLD, Farside, CoinMetrics, blockchain.info,
Etherscan, DefiLlama, погода. Без мережі — на фрагментах РЕАЛЬНИХ
відповідей, знятих живими запитами 2026-10-20 (числа звідти).

Окрема увага — "тихим" збоям: кожен адаптер мусить ГОЛОСНО падати,
коли замість даних прийшло щось інше (помилка API з HTTP 200, HTML
замість CSV, змінений формат сторінки). Порожній список тут читався б
як "даних немає" або "звіряти нічим" — і звірка мовчки вимкнулась би.
"""

import io
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

import config
from commodities.eia_adapter import EiaAdapter
from commodities.spdr_gld_adapter import SpdrGldAdapter, read_sheet_rows
from commodities.tradingeconomics_adapter import TradingEconomicsIndicatorAdapter
from crypto.blockchain_info_adapter import BlockchainInfoAdapter
from crypto.coinmetrics_adapter import CoinMetricsAdapter
from crypto.defillama_adapter import DefiLlamaAdapter
from crypto.etherscan_adapter import EtherscanAdapter
from crypto.farside_adapter import FarsideAdapter, parse_flow
from factor_sources import build_adapter, collection_plan, secondary_pairs
from weather import indices
from weather.met_norway_adapter import daily_from_timeseries, MetNorwayAdapter
from weather.open_meteo_adapter import OpenMeteoAdapter


# --- EIA ----------------------------------------------------------------

def _eia(rows):
    return {"response": {"total": str(len(rows)), "frequency": "weekly", "data": rows}}


def test_eia_normalizes_weekly_crude_stocks():
    records = EiaAdapter("us_crude_stocks", api_key="k").normalize(_eia([
        {"period": "2026-10-09", "series": "WCESTUS1", "value": "415577", "units": "MBBL"},
        {"period": "2026-10-02", "series": "WCESTUS1", "value": "424134", "units": "MBBL"},
    ]))
    assert [(r.observed_at, r.value) for r in records] == [
        (date(2026, 10, 2), Decimal("424134")),
        (date(2026, 10, 9), Decimal("415577")),
    ]
    assert {r.source for r in records} == {"eia"}


def test_eia_error_json_raises():
    """EIA кладе помилку в JSON (інколи з HTTP 200) — це не "немає даних"."""
    with pytest.raises(ValueError, match="помилка API"):
        EiaAdapter("us_crude_stocks", api_key="k").normalize(
            {"error": {"code": "API_KEY_MISSING", "message": "No api_key was supplied."}}
        )


def test_eia_unit_change_raises():
    """Якщо EIA змінить одиниці, ряд мовчки зсунувся б у 1000 разів."""
    with pytest.raises(ValueError, match="одиниці"):
        EiaAdapter("us_natgas_storage", api_key="k").normalize(_eia([
            {"period": "2026-10-09", "series": "NW2_EPG0_SWO_R48_BCF", "value": "3.567", "units": "TCF"},
        ]))


def test_eia_falls_back_to_demo_key(monkeypatch):
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    assert EiaAdapter("us_crude_stocks").api_key == "DEMO_KEY"


# --- TradingEconomics: тижнева зміна запасів ----------------------------

_TE_CRUDE = (
    '<meta name="description" content="Stocks of crude oil in the United States '
    'decreased by 8.56million barrels in the week ending October 9 of 2026. This page '
    'provides the latest reported value for - United States Crude Oil Stocks Change">'
)
_TE_GAS = (
    '<meta name="description" content="Working gas held in storage facilities in the '
    'United States increased by 67 billion cubic feet in the week ending October 9 of '
    '2026 . This page provides the latest reported value">'
)


def test_te_crude_change_in_eia_units_and_sign():
    """Живо 2026-10-20: EIA 415 577 − 424 134 = −8 557 тис. бар.; TE −8.56 млн."""
    (record,) = TradingEconomicsIndicatorAdapter("us_crude_stocks_change").normalize(_TE_CRUDE)
    assert record.value == Decimal("-8560")
    assert record.observed_at == date(2026, 10, 9)


def test_te_gas_change_positive_bcf():
    (record,) = TradingEconomicsIndicatorAdapter("us_natgas_storage_change").normalize(_TE_GAS)
    assert record.value == Decimal("67")
    assert record.observed_at == date(2026, 10, 9)


def test_te_indicator_format_change_raises():
    with pytest.raises(ValueError, match="формат"):
        TradingEconomicsIndicatorAdapter("us_crude_stocks_change").normalize("<html>капча</html>")


def test_te_indicator_scale_mismatch_raises():
    text = _TE_CRUDE.replace("8.56million", "8.56billion")
    with pytest.raises(ValueError, match="масштаб"):
        TradingEconomicsIndicatorAdapter("us_crude_stocks_change").normalize(text)


# --- Farside --------------------------------------------------------------

def _farside_page(rows):
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><tr><td>small</td></tr></table><table>{body}</table>"


_FARSIDE_HEADER = ["Date", "IBIT", "FBTC", "GBTC", "Total"]


def test_farside_parses_totals_with_outflows_in_parentheses():
    page = _farside_page([
        _FARSIDE_HEADER,
        ["14 Oct 2026", "(110.9)", "(152.7)", "2.2", "(261.4)"],
        ["19 Oct 2026", "665.2", "(83.6)", "(54.9)", "526.7"],
        ["Total", "66,233", "10,381", "(27,998)", "57,476"],
    ])
    records = FarsideAdapter("btc_etf_flow_usd_m").normalize(page)
    assert [(r.observed_at, r.value) for r in records] == [
        (date(2026, 10, 14), Decimal("-261.4")),
        (date(2026, 10, 19), Decimal("526.7")),
    ]


def test_farside_skips_unfinished_last_day():
    """Фонд, що напередодні звітував, а сьогодні ще "-" — день не
    закритий; часткова сума не має піти в аналіз як остаточна."""
    page = _farside_page([
        _FARSIDE_HEADER,
        ["16 Oct 2026", "(71.8)", "(10.6)", "0.0", "(82.4)"],
        ["19 Oct 2026", "665.2", "-", "0.0", "665.2"],
    ])
    records = FarsideAdapter("btc_etf_flow_usd_m").normalize(page)
    assert [r.observed_at for r in records] == [date(2026, 10, 16)]


def test_farside_dash_in_old_rows_is_not_incomplete():
    """Фонд, якого ще не існувало ("-" у старих рядках), не робить день
    незакритим — перевіряється лише останній рядок."""
    page = _farside_page([
        _FARSIDE_HEADER,
        ["11 Jan 2024", "111.7", "-", "(95.1)", "16.6"],
        ["12 Jan 2024", "386.0", "-", "(484.1)", "(98.1)"],
    ])
    assert len(FarsideAdapter("btc_etf_flow_usd_m").normalize(page)) == 2


def test_farside_eth_page_without_date_header():
    """Живо: на ETH-сторінці заголовок без "Date" і є рядок "Fee"."""
    page = _farside_page([
        ["", "ETHA", "FETH", "Total"],
        ["Fee", "0.25%", "0.25%", ""],
        ["19 Oct 2026", "(95.4)", "(6.0)", "(103.3)"],
    ])
    (record,) = FarsideAdapter("eth_etf_flow_usd_m").normalize(page)
    assert record.value == Decimal("-103.3")


def test_farside_without_table_raises():
    with pytest.raises(ValueError, match="таблиці"):
        FarsideAdapter("btc_etf_flow_usd_m").normalize("<html>Just a moment...</html>")


def test_parse_flow_variants():
    assert parse_flow("(1,234.5)") == Decimal("-1234.5")
    assert parse_flow("0.0") == Decimal("0")
    assert parse_flow("-") is None
    assert parse_flow("") is None


# --- SPDR GLD (xlsx стандартною бібліотекою) ----------------------------

def _xlsx(rows: list[list[str]], sheet="US GLD Historical Archive") -> bytes:
    """Мінімальний xlsx у тому ж форматі, що архів емітента: рядки
    через sharedStrings, числа — як double (з двійковим шумом)."""
    strings: list[str] = []

    def cell(ref, value):
        if isinstance(value, str):
            strings.append(value)
            return f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>'
        return f'<c r="{ref}"><v>{value}</v></c>'

    xml_rows = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(cell(f"{chr(65 + c)}{r}", v) for c, v in enumerate(row))
        xml_rows.append(f'<row r="{r}">{cells}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("xl/workbook.xml", f'<workbook {ns} {rns}><sheets><sheet name="Disclaimer" sheetId="1" r:id="rId1"/><sheet name="{sheet}" sheetId="2" r:id="rId2"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        zf.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData/></worksheet>")
        zf.writestr("xl/worksheets/sheet2.xml", f"<worksheet {ns}><sheetData>{''.join(xml_rows)}</sheetData></worksheet>")
        zf.writestr("xl/sharedStrings.xml", f"<sst {ns}>" + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
    return buffer.getvalue()


def test_gld_reads_tonnes_and_strips_float_noise():
    """Живо: Excel віддав 1054.20000000000005. Без очищення кожне
    перезбирання могло б давати "ревізію" на шумі останнього знака."""
    data = _xlsx([
        ["Date", "Closing Price", "Tonnes of Gold"],
        ["16-Oct-2026", 389.1, 1059.039999999999964],
        ["17-Oct-2026", "US Holiday", "US Holiday"],
        ["19-Oct-2026", 390.2, 1058.47000000000003],
    ])
    records = SpdrGldAdapter("gld_tonnes").normalize(data)
    assert [(r.observed_at, r.value) for r in records] == [
        (date(2026, 10, 16), Decimal("1059.04")),
        (date(2026, 10, 19), Decimal("1058.47")),
    ]


def test_gld_missing_header_raises():
    with pytest.raises(ValueError, match="заголовків"):
        SpdrGldAdapter("gld_tonnes").normalize(_xlsx([["Date", "Something else"]]))


def test_gld_non_xlsx_raises():
    with pytest.raises(ValueError, match="не xlsx"):
        SpdrGldAdapter("gld_tonnes").normalize(b"<html>moved</html>")


def test_xlsx_reader_finds_sheet_by_name():
    rows = read_sheet_rows(_xlsx([["a", "b"]]), "US GLD Historical Archive")
    assert rows == [{"A": "a", "B": "b"}]


# --- он-чейн ------------------------------------------------------------

def test_coinmetrics_daily_tx_count():
    records = CoinMetricsAdapter("eth_tx_count").normalize({"data": [
        {"asset": "eth", "time": "2026-10-18T00:00:00.000000000Z", "TxCnt": "1595980"},
        {"asset": "eth", "time": "2026-10-19T00:00:00.000000000Z", "TxCnt": "1748461"},
    ]})
    assert [(r.observed_at, r.value) for r in records] == [
        (date(2026, 10, 18), Decimal("1595980")),
        (date(2026, 10, 19), Decimal("1748461")),
    ]


def test_coinmetrics_forbidden_raises():
    """Живо для SOL: "not available with supplied credentials"."""
    with pytest.raises(ValueError, match="помилка API"):
        CoinMetricsAdapter("btc_tx_count").normalize(
            {"error": {"type": "forbidden", "message": "not available with supplied credentials"}}
        )


def test_blockchain_info_unix_day_to_date():
    records = BlockchainInfoAdapter("btc_tx_count").normalize({
        "status": "ok", "values": [{"x": 1791849600, "y": 838363.0}],
    })
    assert records[0].observed_at == datetime.fromtimestamp(1791849600, timezone.utc).date()
    assert records[0].value == Decimal("838363.0")


def test_blockchain_info_bad_status_raises():
    with pytest.raises(ValueError):
        BlockchainInfoAdapter("btc_tx_count").normalize({"status": "error"})


def test_etherscan_csv_tail():
    csv_text = (
        '"Date(UTC)","UnixTimeStamp","Value"\n'
        '"10/18/2026","1792281600","1595980"\n'
        '"10/19/2026","1792368000","1748461"\n'
    )
    adapter = EtherscanAdapter("eth_tx_count")
    adapter._limit = 1
    (record,) = adapter.normalize(csv_text)
    assert record.value == Decimal("1748461")
    assert record.observed_at == date(2026, 10, 19)


def test_etherscan_html_instead_of_csv_raises():
    with pytest.raises(ValueError, match="CSV"):
        EtherscanAdapter("eth_tx_count").normalize("<!DOCTYPE html><title>Just a moment</title>")


def test_defillama_drops_current_unfinished_day():
    payload = [
        {"date": 1792195200, "tvl": 5924234495.4},   # 2026-10-17
        {"date": 1792281600, "tvl": 5896063301.1},   # 2026-10-18
        {"date": 1792368000, "tvl": 5851238346.9},   # 2026-10-19
    ]
    records = DefiLlamaAdapter("sol_chain_tvl").normalize(payload, today=date(2026, 10, 19))
    assert [r.observed_at for r in records] == [date(2026, 10, 17), date(2026, 10, 18)]
    assert records[-1].value == Decimal("5896063301")


# --- погода ---------------------------------------------------------------

def test_hdd_uses_noaa_definition():
    """HDD = 65°F − (Tmax+Tmin)/2 у °F; тепла доба дає 0, не мінус."""
    cold = indices.DailyWeather(date(2026, 10, 21), 10.0, 0.0, 0.0)   # середня 5°C = 41°F
    warm = indices.DailyWeather(date(2026, 10, 22), 30.0, 20.0, 0.0)
    assert indices.point_value(indices.HDD, [cold]) == pytest.approx(24.0)
    assert indices.point_value(indices.HDD, [warm]) == 0.0


def test_min_temp_index_takes_worst_point():
    """Заморозок в одному кавовому регіоні вже б'є по врожаю."""
    assert indices.combine(indices.MIN_TEMP, {"a": 5.0, "b": -1.2, "c": 3.0}) == Decimal("-1.2")
    assert indices.combine(indices.PRECIP, {"a": 10.0, "b": 20.0}) == Decimal("15.0")


def _om_location(start: date, days: int, t_max=20.0, t_min=10.0, precip=1.0):
    times = [(start.toordinal() + i) for i in range(days)]
    return {"daily": {
        "time": [date.fromordinal(t).isoformat() for t in times],
        "temperature_2m_max": [t_max] * days,
        "temperature_2m_min": [t_min] * days,
        "precipitation_sum": [precip] * days,
    }}


def test_open_meteo_horizon_is_tomorrow_plus_six():
    issue = date(2026, 10, 20)
    adapter = OpenMeteoAdapter("vietnam_coffee_precip_7d")
    (record,) = adapter.normalize(_om_location(issue, 9, precip=2.0), issue_date=issue)
    assert record.value == Decimal("14.0")   # 7 діб × 2 мм
    assert record.observed_at == issue


def test_open_meteo_incomplete_horizon_raises():
    """Неповна сума опадів читалась би як суха погода."""
    issue = date(2026, 10, 20)
    with pytest.raises(ValueError, match="неповний прогноз"):
        OpenMeteoAdapter("vietnam_coffee_precip_7d").normalize(_om_location(issue, 5), issue_date=issue)


def _met_entry(moment: str, t_max: float, t_min: float, precip: float):
    return {"time": moment, "data": {"instant": {"details": {}}, "next_6_hours": {"details": {
        "air_temperature_max": t_max, "air_temperature_min": t_min, "precipitation_amount": precip,
    }}}}


def test_met_norway_day_from_four_six_hour_windows():
    """Чотири вікна 00/06/12/18 UTC рівно покривають добу; щогодинні
    записи між ними (перекриті вікна) не рахуються вдруге."""
    series = [
        _met_entry("2026-10-21T00:00:00Z", 12.0, 7.0, 0.5),
        _met_entry("2026-10-21T01:00:00Z", 99.0, -99.0, 99.0),
        _met_entry("2026-10-21T06:00:00Z", 15.0, 8.0, 0.0),
        _met_entry("2026-10-21T12:00:00Z", 16.0, 11.0, 1.5),
        _met_entry("2026-10-21T18:00:00Z", 13.0, 9.0, 0.0),
    ]
    day = daily_from_timeseries(series)[date(2026, 10, 21)]
    assert (day.t_max, day.t_min, day.precip) == (16.0, 7.0, 2.0)


def test_met_norway_partial_day_is_unusable():
    series = [_met_entry("2026-10-21T00:00:00Z", 12.0, 7.0, 0.5)]
    assert daily_from_timeseries(series)[date(2026, 10, 21)].t_max is None


def test_met_norway_requires_user_agent(monkeypatch):
    monkeypatch.delenv("MET_NORWAY_USER_AGENT", raising=False)
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    with pytest.raises(RuntimeError, match="User-Agent"):
        MetNorwayAdapter("us_hdd_7d").fetch()


# --- реєстр джерел факторних рядів ---------------------------------------

def test_every_source_in_plan_has_an_adapter():
    """Новий ряд у config.FACTOR_SERIES без адаптера впав би лише о
    13:00 у продакшені — тут він падає одразу."""
    for source, metric_id in collection_plan():
        adapter = build_adapter(source, metric_id)
        assert adapter.source == source


def test_plan_includes_crosscheck_sources():
    plan = set(collection_plan())
    assert ("eia", "us_crude_stocks") in plan
    assert ("tradingeconomics", "us_crude_stocks_change") in plan
    assert ("blockchain_info", "btc_tx_count") in plan


def test_secondary_pairs_defaults():
    (pair,) = secondary_pairs("btc_tx_count")
    assert pair == {"source": "blockchain_info", "metric_id": "btc_tx_count", "compare": "level"}


def test_factor_series_with_pair_has_tolerance():
    """Ряд із парою без допуску впав би в consensus.reconcile на None."""
    for metric_id, spec in config.FACTOR_SERIES.items():
        if spec["secondary"]:
            assert spec["tolerance_pct"] is not None, metric_id
            kinds = {item.get("compare", "level") for item in spec["secondary"]}
            assert len(kinds) == 1, f"{metric_id}: змішані способи звірки не підтримуються"


# =====================================================================
# Друга хвиля (2026-10-20): OPEC/Саудівська Аравія, газ США, ICE, USDA,
# IAU, резерви ЦБ, стейкінг ETH, Solana, інтервенції Японії
# =====================================================================

from pathlib import Path

import requests

from commodities.ice_coffee_adapter import IceCoffeeAdapter, parse_report
from commodities.ishares_iau_adapter import IsharesIauAdapter
from commodities.tradingeconomics_adapter import TradingEconomicsPeriodAdapter, parse_period_pair
from commodities.usda_psd_adapter import UsdaPsdAdapter
from crypto.beacon_staking_adapter import BeaconStakingAdapter
from crypto.solana_rpc_adapter import SAMPLES, SolanaRpcAdapter
from macro.mof_japan_adapter import MofJapanAdapter, parse_interventions

FIXTURES = Path(__file__).parent / "fixtures"


# --- EIA STEO ---------------------------------------------------------

def test_steo_drops_current_and_future_months():
    """STEO тягне прогноз на ~18 місяців уперед (живо: 2027-12). Прогноз
    EIA — не спостереження, у raw_observations йому не місце."""
    rows = [
        {"period": "2027-12", "seriesId": "COPR_OPEC", "value": "25.165", "unit": "million barrels per day"},
        {"period": "2026-10", "seriesId": "COPR_OPEC", "value": "20.76", "unit": "million barrels per day"},
        {"period": "2026-09", "seriesId": "COPR_OPEC", "value": "20.8", "unit": "million barrels per day"},
        {"period": "2026-08", "seriesId": "COPR_OPEC", "value": "19.86", "unit": "million barrels per day"},
    ]
    records = EiaAdapter("opec_crude_production", api_key="k").normalize(_eia(rows), today=date(2026, 10, 20))
    assert [(r.observed_at, r.value) for r in records] == [
        (date(2026, 8, 1), Decimal("19.86")),
        (date(2026, 9, 1), Decimal("20.8")),
    ]


class _FlakySession:
    """Перший запит висить (як живо ~1 з 5 до api.eia.gov), другий — ок."""

    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise requests.exceptions.Timeout("read timed out")

        class _Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"response": {"data": []}}

        return _Response()


def test_eia_retries_once_on_hang():
    session = _FlakySession(failures=1)
    EiaAdapter("us_crude_stocks", api_key="k", session=session).fetch(limit=1)
    assert session.calls == 2


def test_eia_gives_up_after_second_hang():
    """Лише ОДНА повторна спроба — не нескінченний цикл."""
    session = _FlakySession(failures=5)
    with pytest.raises(requests.exceptions.Timeout):
        EiaAdapter("us_crude_stocks", api_key="k", session=session).fetch(limit=1)
    assert session.calls == 2


# --- TradingEconomics: період ← попередній період ----------------------

_TE_SAUDI = (
    '<meta name="description" content="Crude Oil Production in Saudi Arabia increased to '
    '8372 BBL/D/1K in September from 6238 BBL/D/1K in August of 2026. This page provides">'
)
_TE_CHINA = (
    '<meta name="description" content="Gold Reserves in China increased to 2346.43 Tonnes in '
    'the second quarter of 2026 from 2313.46 Tonnes in the first quarter of 2026. This page provides">'
)
_TE_INDIA_FLAT = (
    '<meta name="description" content="Gold Reserves in India remained unchanged at 880.52 Tonnes '
    'in the second quarter of 2026 from 880.52 Tonnes in the first quarter of 2026. This page">'
)


def test_te_saudi_in_eia_units_both_points():
    """Тис. бар./добу → млн, як у EIA STEO, інакше звірка порівнювала б
    8372 з 8.1. Живо: вересень 8.372 проти EIA 8.10."""
    records = TradingEconomicsPeriodAdapter("saudi_crude_production").normalize(_TE_SAUDI, today=date(2026, 10, 20))
    assert [(r.observed_at, r.value) for r in records] == [
        (date(2026, 8, 1), Decimal("6.238")),
        (date(2026, 9, 1), Decimal("8.372")),
    ]


def test_te_gold_reserves_quarter_end_dates():
    records = TradingEconomicsPeriodAdapter("china_gold_reserves").normalize(_TE_CHINA, today=date(2026, 10, 20))
    assert [(r.observed_at, r.value) for r in records] == [
        (date(2026, 3, 31), Decimal("2313.46")),
        (date(2026, 6, 30), Decimal("2346.43")),
    ]


def test_te_unchanged_wording_is_parsed():
    adapter = TradingEconomicsPeriodAdapter("china_gold_reserves")
    records = adapter.normalize(_TE_INDIA_FLAT, today=date(2026, 10, 20))
    assert [r.value for r in records] == [Decimal("880.52"), Decimal("880.52")]


def test_te_period_unit_mismatch_raises():
    with pytest.raises(ValueError, match="одиниця"):
        TradingEconomicsPeriodAdapter("saudi_crude_production").normalize(_TE_CHINA, today=date(2026, 10, 20))


def test_month_year_boundary_never_lands_in_future():
    """TE пише рік лише в кінці — на межі року незрозуміло, до якого
    місяця він належить. Береться варіант не з майбутнього."""
    for text in (("January", "December of 2025"), ("January of 2026", "December")):
        assert parse_period_pair(*text, today=date(2026, 2, 10)) == (date(2026, 1, 1), date(2025, 12, 1))


# --- ICE: сертифіковані запаси кави (реальний звіт 2026-10-16) --------

def test_ice_report_total_certified_bags():
    content = (FIXTURES / "ice_coffee_cert_stock_20261016.xls").read_bytes()
    assert parse_report(content, "TOTAL BAGS CERTIFIED") == (date(2026, 10, 16), Decimal("240767"))


def test_ice_report_missing_section_raises():
    content = (FIXTURES / "ice_coffee_cert_stock_20261016.xls").read_bytes()
    with pytest.raises(ValueError, match="секції"):
        parse_report(content, "NO SUCH SECTION")


class _IceSession:
    """Звіт є лише в будні; у вихідні ICE віддає 404 (живо)."""

    def __init__(self, content, limit_after=None):
        self.content = content
        self.requested = []
        self.limit_after = limit_after

    def get(self, url, headers=None, timeout=None):
        self.requested.append(url)
        day = date(int(url[-12:-8]), int(url[-8:-6]), int(url[-6:-4]))

        class _Response:
            pass

        response = _Response()
        response.status_code = 200 if day.weekday() < 5 else 404
        if self.limit_after is not None and len(self.requested) > self.limit_after:
            response.status_code = 429

        def _raise():
            if response.status_code >= 400 and response.status_code != 404:
                raise requests.exceptions.HTTPError(f"{response.status_code}")
        response.content = self.content if response.status_code == 200 else b"<html>404</html>"
        response.raise_for_status = _raise
        return response


@pytest.fixture(autouse=False)
def no_ice_pause(monkeypatch):
    import commodities.ice_coffee_adapter as ice
    monkeypatch.setattr(ice, "_REQUEST_PAUSE_SECONDS", 0)


def test_ice_rate_limit_mid_backfill_keeps_fresh_reports(no_ice_pause):
    """Живо 2026-10-20: добір 120 днів → 429 від ICE. Свіжі звіти, уже
    скачані, не мають пропасти через старіші."""
    content = (FIXTURES / "ice_coffee_cert_stock_20261016.xls").read_bytes()
    session = _IceSession(content, limit_after=2)
    files = IceCoffeeAdapter("coffee_certified_bags", session=session).fetch(limit=10, today=date(2026, 10, 16))
    assert len(files) == 2


def test_ice_rate_limit_before_any_report_raises(no_ice_pause):
    content = (FIXTURES / "ice_coffee_cert_stock_20261016.xls").read_bytes()
    session = _IceSession(content, limit_after=0)
    with pytest.raises(requests.exceptions.HTTPError):
        IceCoffeeAdapter("coffee_certified_bags", session=session).fetch(limit=5, today=date(2026, 10, 16))


def test_ice_fetch_walks_back_over_weekend(no_ice_pause):
    content = (FIXTURES / "ice_coffee_cert_stock_20261016.xls").read_bytes()
    session = _IceSession(content)
    files = IceCoffeeAdapter("coffee_certified_bags", session=session).fetch(limit=2, today=date(2026, 10, 18))
    assert len(files) == 2
    assert [u[-12:-4] for u in session.requested] == ["20261018", "20261017", "20261016", "20261015"]


def test_ice_no_reports_raises():
    with pytest.raises(ValueError, match="жодного звіту"):
        IceCoffeeAdapter("coffee_certified_bags").normalize([])


# --- USDA PSD -----------------------------------------------------------

def _psd_zip(rows):
    import csv

    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow([
        "Commodity_Code", "Commodity_Description", "Country_Code", "Country_Name", "Market_Year",
        "Calendar_Year", "Month", "Attribute_ID", "Attribute_Description", "Unit_ID", "Unit_Description", "Value",
    ])
    writer.writerows(rows)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("psd_coffee.csv", text.getvalue())
    return buffer.getvalue()


def test_usda_psd_brazil_production_integer_bags():
    """Живо: Бразилія 2026 — 71 900 тис. мішків. Без приведення
    "71900.0000" перетворювався на "7.19E+4"."""
    data = _psd_zip([
        ["0711100", "Coffee, Green", "BR", "Brazil", "2025", "2026", "07", "28", "Production", "2", "(1000 60 KG BAGS)", "63000.0000"],
        ["0711100", "Coffee, Green", "BR", "Brazil", "2026", "2026", "07", "28", "Production", "2", "(1000 60 KG BAGS)", "71900.0000"],
        ["0711100", "Coffee, Green", "BR", "Brazil", "2026", "2026", "07", "176", "Ending Stocks", "2", "(1000 60 KG BAGS)", "4425.0000"],
        ["0711100", "Coffee, Green", "VM", "Vietnam", "2026", "2026", "07", "28", "Production", "2", "(1000 60 KG BAGS)", "32500.0000"],
    ])
    records = UsdaPsdAdapter("brazil_coffee_production").normalize(data)
    assert [(r.observed_at, str(r.value)) for r in records] == [
        (date(2025, 1, 1), "63000"),
        (date(2026, 1, 1), "71900"),
    ]
    assert records[-1].raw_payload["published"] == "2026-07"


def test_usda_psd_unit_change_raises():
    data = _psd_zip([["0711100", "Coffee, Green", "BR", "Brazil", "2026", "2026", "07", "28", "Production", "2", "(METRIC TONS)", "4314000"]])
    with pytest.raises(ValueError, match="одиниці"):
        UsdaPsdAdapter("brazil_coffee_production").normalize(data)


# --- iShares IAU ----------------------------------------------------------

def _iau_page(tonnes="462.93", ounces="14,883,534.15"):
    # Як на живій сторінці: JSON в атрибуті HTML, екранований &quot;.
    blob = (
        f'"label":"Tonnes in Trust","formattedValue":"{tonnes}","sortOrder":48,"formattedAsOfDate":"Oct 19, 2026",'
        f'"label":"Ounces in Trust","formattedValue":"{ounces}","sortOrder":49,"formattedAsOfDate":"Oct 19, 2026"'
    )
    return f'<div componentprops="{blob.replace(chr(34), "&quot;")}"></div>'


def test_iau_reads_escaped_json():
    (record,) = IsharesIauAdapter("iau_tonnes").normalize(_iau_page())
    assert (record.observed_at, record.value) == (date(2026, 10, 19), Decimal("462.93"))


def test_iau_inconsistent_tonnes_and_ounces_raise():
    with pytest.raises(ValueError, match="не сходяться"):
        IsharesIauAdapter("iau_tonnes").normalize(_iau_page(tonnes="500.00"))


# --- Мінфін Японії --------------------------------------------------------

_MOF_CSV = (
    "(amount:100 million Yen),,,,,,,,\n"
    "令和6年4月～6月期計,,,April - June 2024,,,\"97,885\",,\n"
    "令和6年,7月,11日,2024,Jul,11,\"31,678\",米ドル売り・日本円買い,the US dollar (sold) the Japanese yen (bought)\n"
    ",7月,12日,,Jul,12,\"23,670\",米ドル売り・日本円買い,the US dollar (sold) the Japanese yen (bought)\n"
    "令和8年,4月,30日,2026,Apr,30,\"62,787\",米ドル売り・日本円買い,the US dollar (sold) the Japanese yen (bought)\n"
    "平成10年,4月,10日,1998,Apr,10,\"1,000\",x,the Japanese yen (sold) the US dollar (bought)\n"
)


def test_mof_carries_year_and_signs_yen_buying_positive():
    """Рік у другому рядку кварталу порожній — переноситься; купівля
    ієни = +, продаж = −; квартальні підсумки — не дні."""
    days = [(d, v) for d, v, _ in parse_interventions(_MOF_CSV)]
    assert days == [
        (date(2024, 7, 11), Decimal("31678")),
        (date(2024, 7, 12), Decimal("23670")),
        (date(2026, 4, 30), Decimal("62787")),
        (date(1998, 4, 10), Decimal("-1000")),
    ]


def test_mof_records_sorted_by_date():
    records = MofJapanAdapter("japan_fx_intervention").normalize(_MOF_CSV)
    assert [r.observed_at for r in records] == sorted(r.observed_at for r in records)


def test_mof_format_change_raises():
    with pytest.raises(ValueError, match="одиниць"):
        parse_interventions("Year,Month,Day,Amount\n2026,Apr,30,100\n")


# --- стейкінг ETH і Solana ---------------------------------------------

def test_beacon_active_gwei_to_eth():
    """Живо: 43 760 823 000 000 000 gwei = 43 760 823 ETH."""
    (record,) = BeaconStakingAdapter("eth_staked").normalize(
        {"epoch": 483486, "body": {"data": {"current_epoch_active_gwei": 43760823000000000}}}
    )
    assert record.value == Decimal("43760823")


def test_beacon_missing_field_raises():
    with pytest.raises(ValueError, match="current_epoch_active_gwei"):
        BeaconStakingAdapter("eth_staked").normalize({"epoch": 1, "body": {"code": 404}})


def _samples(n, per_sample=33860, secs=60):
    return {"result": [{"slot": 1000 - i, "numNonVoteTransactions": per_sample, "samplePeriodSecs": secs} for i in range(n)]}


def test_solana_rate_per_day_over_fixed_window():
    (record,) = SolanaRpcAdapter("sol_nonvote_tx_per_day").normalize(_samples(SAMPLES))
    assert record.value == Decimal(33860 * 1440)
    assert record.source == "solana_publicnode"


def test_solana_short_window_is_not_comparable():
    """Живо: офіційний вузол тримав 270 зразків, publicnode — 720, і
    різні вікна дали 12% різниці на тих самих даних."""
    with pytest.raises(ValueError, match="не співставне"):
        SolanaRpcAdapter("sol_nonvote_tx_per_day", source="solana_mainnet").normalize(_samples(SAMPLES - 1))


def test_solana_two_nodes_are_two_sources():
    assert build_adapter("solana_mainnet", "sol_nonvote_tx_per_day").source == "solana_mainnet"
    assert build_adapter("solana_publicnode", "sol_nonvote_tx_per_day").source == "solana_publicnode"
