import pytest

from news.queries import (
    GENERAL_TERMS,
    GENERAL_TERMS_EVENTS,
    GENERAL_TERMS_MOVEMENT,
    MAX_QUERY_LEN,
    STOCK_NAME_OVERRIDES,
    WATCHLIST_ASSET_IDS,
    WATCHLIST_TERMS,
    batch_ticker_names,
    build_general_queries,
    build_stocks_query,
    build_watchlist_query,
)


def test_build_watchlist_query_includes_every_asset_term():
    query = build_watchlist_query()
    for term in WATCHLIST_TERMS.values():
        assert term in query


def test_build_watchlist_query_is_a_single_or_group():
    query = build_watchlist_query()
    assert query.startswith("(")
    assert query.endswith(")")
    assert " OR " in query


def test_watchlist_asset_ids_match_watchlist_terms_keys():
    # Одне джерело істини для "що таке watchlist" — query і
    # tracked_assets (analysis/news_analysis/) не повинні розійтись.
    assert WATCHLIST_ASSET_IDS == list(WATCHLIST_TERMS)


def test_build_watchlist_query_uses_explicit_terms_when_given():
    # 2026-10-03: run_collect_news.py передає живий словник з БД
    # (common/watchlist_db.py:fetch_terms) замість статичного фолбеку.
    query = build_watchlist_query({"gbpusd": '"GBP/USD"'})
    assert query == '("GBP/USD")'


def test_build_stocks_query_uses_company_name_when_known():
    query = build_stocks_query({"NVDA": "NVIDIA Corporation", "AAPL": "Apple Inc."})
    assert '"NVIDIA Corporation"' in query
    assert '"Apple Inc."' in query
    assert "NVDA" not in query  # тикер сам по собі не термін, коли є назва


def test_build_stocks_query_falls_back_to_ticker_when_name_missing():
    query = build_stocks_query({"BX": ""})
    assert query == "(BX)"


def test_build_stocks_query_rejects_empty_input():
    with pytest.raises(ValueError):
        build_stocks_query({})


def test_build_stocks_query_applies_uber_override():
    # Regression: живий прогін 2026-09-25 підтвердив, що GDELT
    # відхиляє "Uber" сам по собі як "too short" (двічі, незалежно
    # від довжини решти query) — STOCK_NAME_OVERRIDES виправляє це
    # конкретне слово повною офіційною назвою компанії.
    assert STOCK_NAME_OVERRIDES["UBER"] == "Uber Technologies"
    query = build_stocks_query({"UBER": "Uber"})
    assert query == '("Uber Technologies")'


def test_build_stocks_query_override_takes_priority_over_given_name():
    # Навіть якщо викликач передав інакшу (напр. застарілу) назву,
    # override для відомого проблемного тикера має виграти.
    query = build_stocks_query({"UBER": "Some Other Name"})
    assert query == '("Uber Technologies")'


def test_build_general_queries_returns_two_queries():
    # Regression 2026-09-28: об'єднаний одинарний query (15 термінів,
    # 266 символів) перевищував MAX_QUERY_LEN — розбито на дві групи.
    queries = build_general_queries()
    assert len(queries) == 2


def test_build_general_queries_together_include_every_term():
    queries = build_general_queries()
    combined = " ".join(queries)
    for term in GENERAL_TERMS.values():
        assert term in combined


def test_build_general_queries_are_each_a_single_or_group():
    for query in build_general_queries():
        assert query.startswith("(")
        assert query.endswith(")")
        assert " OR " in query


def test_build_general_queries_terms_are_not_single_short_common_words():
    for term in GENERAL_TERMS.values():
        bare = term.strip('"')
        assert " " in bare or bare.isupper()


def test_build_general_queries_each_under_max_len():
    for query in build_general_queries():
        assert len(query) <= MAX_QUERY_LEN


def test_general_terms_groups_are_disjoint_and_cover_general_terms():
    assert set(GENERAL_TERMS_MOVEMENT) & set(GENERAL_TERMS_EVENTS) == set()
    assert {**GENERAL_TERMS_MOVEMENT, **GENERAL_TERMS_EVENTS} == GENERAL_TERMS


def test_general_terms_cover_market_decline_and_major_indices():
    # Regression 2026-09-28: живий фідбек користувача — падіння ринку
    # того дня не потрапило в raw_news взагалі, бо термінів на кшталт
    # "stocks plunge"/"S&P 500" не було в GENERAL_TERMS.
    combined = " ".join(GENERAL_TERMS.values())
    for must_have in ('"stocks plunge"', '"stocks tumble"', '"S&P 500"', '"Dow Jones"', "NASDAQ"):
        assert must_have in combined


# Тикери/назви з реального live-прогону 2026-09-25 (Tier C), що
# спричинили GDELT "Your query was too short or too long" одним
# об'єднаним запитом (294 символи) — regression fixture для batching.
_LIVE_TIER_C_TICKER_NAMES = {
    "BX": "Blackstone Inc.",
    "CASY": "Casey's",
    "COP": "ConocoPhillips",
    "EME": "Emcor",
    "FANG": "Diamondback Energy",
    "GNRC": "Generac",
    "HAL": "Halliburton",
    "HII": "Huntington Ingalls Industries",
    "HPE": "Hewlett Packard Enterprise",
    "IVZ": "Invesco",
    "KMI": "Kinder Morgan",
    "MDT": "Medtronic",
    "STLD": "Steel Dynamics",
    "TEL": "TE Connectivity",
    "UBER": "Uber",
    "WAB": "Wabtec",
}


def test_batch_ticker_names_empty():
    assert batch_ticker_names({}) == []


def test_batch_ticker_names_single_ticker_never_split():
    batches = batch_ticker_names({"UBER": "Uber"})
    assert batches == [{"UBER": "Uber"}]


def test_batch_ticker_names_every_batch_query_at_least_the_length_floor():
    # Регресія на реальний збій: 41-символьний "хвіст" GDELT відхилив
    # як "too short" -- жодна група не повинна вийти коротшою за поріг.
    batches = batch_ticker_names(_LIVE_TIER_C_TICKER_NAMES, max_query_len=150, min_query_len=70)
    for batch in batches:
        assert len(build_stocks_query(batch)) >= 70


def test_batch_ticker_names_non_final_batches_respect_max_length():
    # Останній батч може перевищити max_query_len ПІСЛЯ злиття замалого
    # хвоста (нижче за поріг), але не більш ніж на ще один max_query_len
    # -- решта батчів лишаються в межах ліміту як і раніше.
    batches = batch_ticker_names(_LIVE_TIER_C_TICKER_NAMES, max_query_len=150, min_query_len=70)
    assert len(batches) > 1  # 294 символи одним запитом -- має розбитись
    for batch in batches[:-1]:
        assert len(build_stocks_query(batch)) <= 150
    assert len(build_stocks_query(batches[-1])) <= 150 + 70


def test_batch_ticker_names_merges_too_short_trailing_batch():
    # Синтетичний приклад: перша група заповнюється до ліміту, друга
    # (хвіст) лишається замалою -- має приєднатись до першої, а не
    # лишитись окремим запитом.
    names = {
        "A": "Alpha Corporation Holdings",  # ~30 символів сам по собі
        "B": "Beta",
    }
    batches = batch_ticker_names(names, max_query_len=35, min_query_len=20)
    assert batches == [{"A": "Alpha Corporation Holdings", "B": "Beta"}]


def test_batch_ticker_names_no_merge_when_last_batch_already_long_enough():
    names = {"A": "Alpha Corporation Holdings", "B": "Beta Industries Group"}
    batches = batch_ticker_names(names, max_query_len=35, min_query_len=5)
    assert len(batches) == 2  # обидва батчі вже довші за поріг -- злиття не потрібне


def test_batch_ticker_names_preserves_every_ticker_exactly_once():
    batches = batch_ticker_names(_LIVE_TIER_C_TICKER_NAMES, max_query_len=150)
    seen = {}
    for batch in batches:
        seen.update(batch)
    assert seen == _LIVE_TIER_C_TICKER_NAMES
