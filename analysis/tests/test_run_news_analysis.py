from news_analysis.run_news_analysis import resolve_tracked_assets

# Фікстура, що раніше йшла статично з news/queries.py:WATCHLIST_ASSET_IDS
# (2026-10-03: тепер main() рахує це живим викликом
# common/watchlist_db.py:fetch_asset_ids(conn) і передає явно).
_DEFAULTS = {"watchlist": ["btc", "xauusd"]}

# `watchlist_assets.news_patterns` (2026-10-22, доти
# config.WATCHLIST_MATCH_TERMS) — у тестах лише потрібні активи.
_PATTERNS = {
    "xauusd": (r"\bgold\b", r"\bbullion\b", r"\bxau\b"),
    "brent_crude": (r"\bbrent\b", r"\bcrude\b", r"\boil price", r"\bopec\b", r"\bhormuz\b"),
    "btc": (r"\bbitcoin\b", r"\bbtc\b"),
    "skyd": (r"\bskydance\b", r"\bskyd\b"),
}


def test_explicit_overrides_default():
    result = resolve_tracked_assets("watchlist", ["AAPL", "NVDA"], default_tracked_assets=_DEFAULTS)
    assert result == ["AAPL", "NVDA"]


def test_watchlist_falls_back_to_default_tracked_assets():
    result = resolve_tracked_assets("watchlist", None, default_tracked_assets=_DEFAULTS)
    assert result is not None
    assert "btc" in result
    assert "xauusd" in result


def test_general_has_no_default():
    assert resolve_tracked_assets("general", None, default_tracked_assets=_DEFAULTS) is None


def test_geopolitical_has_no_default():
    assert resolve_tracked_assets("geopolitical", None, default_tracked_assets=_DEFAULTS) is None


def test_no_default_tracked_assets_given_returns_none():
    # default_tracked_assets не передано взагалі (None) — той самий
    # ефект, що порожній словник, не падає.
    assert resolve_tracked_assets("watchlist", None) is None


def test_watchlist_includes_screening_tickers_when_provided():
    result = resolve_tracked_assets(
        "watchlist", None, screening_tickers=["AAPL", "NVDA"], default_tracked_assets=_DEFAULTS
    )
    assert "AAPL" in result
    assert "NVDA" in result
    assert "btc" in result  # дефолтний watchlist усе одно на місці


def test_watchlist_without_screening_tickers_unchanged():
    result = resolve_tracked_assets(
        "watchlist", None, screening_tickers=None, default_tracked_assets=_DEFAULTS
    )
    assert "AAPL" not in result
    assert "btc" in result


def test_screening_tickers_ignored_when_stream_has_no_default():
    result = resolve_tracked_assets(
        "general", None, screening_tickers=["AAPL"], default_tracked_assets=_DEFAULTS
    )
    assert result is None


def test_explicit_overrides_even_with_screening_tickers():
    result = resolve_tracked_assets(
        "watchlist", ["AAPL"], screening_tickers=["NVDA"], default_tracked_assets=_DEFAULTS
    )
    assert result == ["AAPL"]


# ---- Резерв watchlist-потоку: пошук по власному корпусу (2026-10-12) ----

def test_geopolitical_without_match_stays_untagged():
    """Вихідне заперечення 2026-10-06 лишається в силі: широкі
    редакційні стрічки не тегуються цілком, інакше стаття про вибори
    отримала б asset_id=eurusd."""
    assert resolve_tracked_assets(
        "geopolitical", None, ["AAPL"],
        default_tracked_assets={"watchlist": ["btc"], "general": ["btc"]},
        title="Voters head to the polls in tight parliamentary race",
        patterns_by_asset=_PATTERNS,
    ) is None


def test_geopolitical_with_match_gets_only_matched_assets():
    """Живий кейс 12 жовтня: історії про Ормузьку протоку лежали в
    geopolitical невидимі для синтезу, поки Brent робив +5%."""
    result = resolve_tracked_assets(
        "geopolitical", None, ["AAPL"],
        default_tracked_assets={"watchlist": ["btc"], "general": ["btc"]},
        title="Oil tanker reportedly attacked in Strait of Hormuz region",
        patterns_by_asset=_PATTERNS,
    )
    assert result is not None
    assert "brent_crude" in result
    # ТІЛЬКИ збіги — не весь watchlist і не тикери скринінгу
    assert "btc" not in result
    assert "AAPL" not in result


def test_match_is_prefilter_not_assignment():
    """Хибний детермінований збіг допускається свідомо — він лише
    ставить питання моделі, asset_id призначає DeepSeek."""
    from news_analysis.run_news_analysis import matched_watchlist_assets
    assert matched_watchlist_assets(
        "Deadly gold mine air strikes highlight Egypt-Sudan border insecurity", _PATTERNS
    ) == ["xauusd"]


def test_telegram_added_stock_is_found_in_headlines():
    """Живий кейс 2026-10-22: акція SKYD, додана через Telegram, у
    пошук по заголовках не потрапляла — терміни жили в config поіменно."""
    from news_analysis.run_news_analysis import matched_watchlist_assets
    assert matched_watchlist_assets(
        "Skydance shares jump after streaming deal", _PATTERNS
    ) == ["skyd"]


def test_explicit_tracked_assets_still_wins_over_match():
    assert resolve_tracked_assets(
        "geopolitical", ["NVDA"], None,
        default_tracked_assets={},
        title="Brent crude jumps on supply fears",
    ) == ["NVDA"]
