"""
Тести TradingEconomicsAdapter на фрагментах реального HTML (meta-опис,
живо підтверджений curl 2026-10-04) — жодних реальних мережевих
викликів.
"""

from datetime import date
from decimal import Decimal

import pytest

from commodities.tradingeconomics_adapter import TradingEconomicsAdapter

COFFEE_HTML = (
    '<meta id="metaDesc" name="description" content="Coffee rose to 288.75 USd/Lbs on October 2, 2026, '
    "up 0.17% from the previous day. Over the past month, Coffee&#39;s price has fallen 2.23%, "
    'and is down 26.10% compared to the same time last year." />'
)

WTI_HTML = (
    '<meta id="metaDesc" name="description" content="Crude Oil fell to 91.11 USD/Bbl on October 2, 2026, '
    'down 1.90% from the previous day." />'
)

BRENT_HTML = (
    '<meta id="metaDesc" name="description" content="Brent fell to 102.25 USD/Bbl on October 2, 2026, '
    'down 0.06% from the previous day." />'
)

NATGAS_HTML = (
    '<meta id="metaDesc" name="description" content="Natural gas rose to 3.04 USD/MMBtu on October 2, 2026, '
    'up 2.29% from the previous day." />'
)

GOLD_HTML = (
    '<meta id="metaDesc" name="description" content="Gold fell to 4,140.19 USD/t.oz on October 2, 2026, '
    'down 0.90% from the previous day." />'
)


def test_unknown_metric_id_without_slug_rejected():
    with pytest.raises(ValueError):
        TradingEconomicsAdapter(metric_id="unknown_commodity")


def test_explicit_slug_bypasses_metrics():
    adapter = TradingEconomicsAdapter(metric_id="custom", slug="some-slug")
    assert adapter.slug == "some-slug"


def test_normalize_parses_coffee_price_and_date():
    adapter = TradingEconomicsAdapter(metric_id="coffee")
    records = adapter.normalize(COFFEE_HTML)

    assert len(records) == 1
    record = records[0]
    assert record.source == "tradingeconomics"
    assert record.metric_id == "coffee"
    assert record.value == Decimal("288.75")
    assert record.observed_at == date(2026, 10, 2)
    assert record.revision is None


def test_normalize_parses_wti_price_fell_direction():
    # "fell to" -- той самий regex має ловити й падіння, не лише "rose to".
    adapter = TradingEconomicsAdapter(metric_id="wti_crude")
    records = adapter.normalize(WTI_HTML)

    assert records[0].value == Decimal("91.11")
    assert records[0].observed_at == date(2026, 10, 2)


def test_normalize_parses_brent_and_natgas():
    brent = TradingEconomicsAdapter(metric_id="brent_crude").normalize(BRENT_HTML)
    natgas = TradingEconomicsAdapter(metric_id="natgas").normalize(NATGAS_HTML)

    assert brent[0].value == Decimal("102.25")
    assert natgas[0].value == Decimal("3.04")


def test_normalize_parses_gold_price_with_thousands_comma():
    # "4,140.19" -- кома-роздільник тисяч, на відміну від решти товарів
    # (значення < 1000) -- regex/parsing має прибрати кому перед Decimal().
    adapter = TradingEconomicsAdapter(metric_id="xauusd")
    records = adapter.normalize(GOLD_HTML)

    assert len(records) == 1
    assert records[0].metric_id == "xauusd"
    assert records[0].value == Decimal("4140.19")
    assert records[0].observed_at == date(2026, 10, 2)


def test_normalize_returns_empty_list_when_format_unexpected():
    # Живий урок Stooq (docs/decisions.md 2026-09-20): неофіційне
    # джерело може змінити формат -- не падати, логувати й пропустити.
    adapter = TradingEconomicsAdapter(metric_id="coffee")
    records = adapter.normalize("<html><body>totally different page</body></html>")
    assert records == []


def test_normalize_handles_non_string_response():
    adapter = TradingEconomicsAdapter(metric_id="coffee")
    assert adapter.normalize(None) == []
