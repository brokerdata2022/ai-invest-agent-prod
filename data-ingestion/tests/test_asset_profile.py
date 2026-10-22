"""
Профіль активу (`common/asset_profile.py`, 2026-10-22) — чисті функції,
без мережі. Приклади — живі відповіді Twelve Data `symbol_search` і SEC
того дня.
"""

import re

import pytest

from common import asset_profile as ap


@pytest.mark.parametrize("instrument_type,exchange,expected", [
    ("Common Stock", "NYSE", ("stock", None)),          # SKYD — доти показано "товаром"
    ("ETF", "NYSE", ("stock", None)),                   # GLD
    ("Physical Currency", "PHYSICAL CURRENCY", ("fx", None)),
    ("Digital Currency", None, ("crypto", None)),
    ("Precious Metal", "COMMODITY", ("commodity", "metal")),
    ("Energy Resource", "COMMODITY", ("commodity", "energy")),
    ("Index", None, ("index", None)),
])
def test_classify_instrument_live_types(instrument_type, exchange, expected):
    assert ap.classify_instrument(instrument_type, exchange) == expected


def test_unknown_instrument_type_is_not_guessed():
    """Хибний клас тихо псує все, що з нього випливає (SKYD-"товар");
    невідомий — видно в /watchlist_list."""
    assert ap.classify_instrument("Mutual Fund", "LSE") == (None, None)
    assert ap.classify_instrument(None) == (None, None)


@pytest.mark.parametrize("raw,clean", [
    ("Skydance Corporation Class B Common Stock", "Skydance"),
    ("Skydance Corp", "Skydance"),
    ("Apple Inc.", "Apple"),
    ("Gold Spot / US Dollar", "Gold"),
    ("Berkshire Hathaway Inc. Class B", "Berkshire Hathaway"),
])
def test_clean_name(raw, clean):
    assert ap.clean_name(raw) == clean


def _matches(patterns, text):
    return any(re.search(p, text.lower()) for p in patterns)


def test_stock_news_patterns_catch_company_name_and_ticker():
    patterns = ap.news_patterns("stock", "skyd", "SKYD", "Skydance Corp")
    assert _matches(patterns, "Skydance shares jump after streaming deal")
    assert _matches(patterns, "SKYD falls 5% premarket")
    assert not _matches(patterns, "Oil prices rise on supply fears")


def test_two_letter_ticker_is_not_a_search_term():
    """'BX'/'GE' збігаються з абревіатурами майже в кожному заголовку."""
    patterns = ap.news_patterns("stock", "ge", "GE", "GE Aerospace")
    assert r"\bge\b" not in patterns


def test_fx_and_crypto_patterns():
    assert _matches(ap.news_patterns("fx", "gbpusd", "GBP/USD", None), "GBP/USD slips after BoE")
    assert _matches(ap.news_patterns("crypto", "bnb", "BNBUSDT", None), "BNB hits record")


def test_ask_patterns_include_asset_id():
    assert _matches(ap.ask_patterns("stock", "skyd", "SKYD", "Skydance Corp"), "що по skyd?")


@pytest.mark.parametrize("asset_class,ticker,asset_id,symbol", [
    ("stock", "SKYD", "skyd", "SKYD"),
    ("stock", "BRK.B", "brkb", "BRK-B"),
    ("fx", "GBP/USD", "gbpusd", "GBPUSD=X"),
    ("crypto", "BNBUSDT", "bnb", "BNB-USD"),
    ("commodity", "XPT/USD", "xptusd", None),   # символ ф'ючерса не виводиться з тикера
])
def test_yahoo_symbol(asset_class, ticker, asset_id, symbol):
    assert ap.yahoo_symbol(asset_class, ticker, asset_id) == symbol
