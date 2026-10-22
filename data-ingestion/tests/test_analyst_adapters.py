"""
Консенсус аналітиків і free float (`companies/analyst_adapters.py`,
2026-10-22) — фікстури з живої розмітки трьох джерел по SKYD.
"""

from decimal import Decimal

import pytest

from companies.analyst_adapters import (
    FinvizAdapter,
    NasdaqAnalystAdapter,
    StockAnalysisAdapter,
    parse_number,
)


def _values(records):
    return {r.metric_id: r.value for r in records}


@pytest.mark.parametrize("text,expected", [
    ("536.71M", Decimal("536710000")),
    ("1.09B", Decimal("1090000000")),
    ("52.27%", Decimal("52.27")),
    ("$8.87", Decimal("8.87")),
    ("4,100,069,013", Decimal("4100069013")),
    ("-", None),
    ("", None),
])
def test_parse_number(text, expected):
    assert parse_number(text) == expected


def test_parse_number_gives_integer_share_counts_not_exponent():
    # Урок USDA "7.19E+4" (2026-10-20): ціле число акцій, не 5.3671E+8
    assert str(parse_number("536.71M")) == "536710000"


def test_nasdaq_consensus():
    raw = {"data": {"consensusOverview": {
        "lowPriceTarget": 6.0, "highPriceTarget": 16.0, "priceTarget": 9.34,
        "buy": 2, "sell": 4, "hold": 5,
    }}}
    values = _values(NasdaqAnalystAdapter("SKYD").normalize(raw))
    assert values["skyd_target_mean"] == Decimal("9.34")
    assert values["skyd_analyst_count"] == Decimal("11")
    assert values["skyd_rating_sell"] == Decimal("4")


def test_nasdaq_changed_format_fails_loudly():
    with pytest.raises(ValueError):
        NasdaqAnalystAdapter("SKYD").normalize({"data": None})


FINVIZ = (
    '<td data-boxover-html="Shares float"><div class="snapshot-td-label">Shs Float</div></td>'
    '<td class="snapshot-td2" align="left"><div class="snapshot-td-content"><b>536.71M</b></div></td>'
    '<td><div class="snapshot-td-label"><a href="x" class="hover:underline">Target Price</a></div></td>'
    '<td class="snapshot-td2"><div class="snapshot-td-content"><a href="x"><b><span class="is-positive">8.96</span></b></a></div></td>'
    '<td><div class="snapshot-td-label">Insider Own</div></td>'
    '<td class="snapshot-td2"><div class="snapshot-td-content"><b><span>52.27%</span></b></div></td>'
)


def test_finviz_reads_labels_with_and_without_links():
    """Живий дефект 2026-10-22: перша версія регулярки знаходила лише
    підписи-посилання й тихо губила free float."""
    values = _values(FinvizAdapter("SKYD").normalize(FINVIZ))
    assert values["skyd_float_shares"] == Decimal("536710000")
    assert values["skyd_target_mean"] == Decimal("8.96")
    assert values["skyd_insider_own_pct"] == Decimal("52.27")


def test_finviz_changed_markup_fails_loudly():
    with pytest.raises(ValueError):
        FinvizAdapter("SKYD").normalize("<html>blocked</html>")


STOCKANALYSIS = (
    '{id:"sharesout",title:"Shares Outstanding",value:"5.04B",hover:"5,042,178,087"},'
    '{id:"float",title:"Float",value:"4.10B",hover:"4,100,069,013"},'
    '{id:"priceTarget",title:"Price Target",value:"$8.87",hover:"$8.87"},'
    '{id:"analystCount",title:"Analyst Count",value:"22",hover:"22"}'
)


def test_stockanalysis_takes_exact_hover_value():
    values = _values(StockAnalysisAdapter("SKYD").normalize(STOCKANALYSIS))
    assert values["skyd_float_shares"] == Decimal("4100069013")
    assert values["skyd_shares_outstanding"] == Decimal("5042178087")
    assert values["skyd_target_mean"] == Decimal("8.87")
    assert values["skyd_analyst_count"] == Decimal("22")
