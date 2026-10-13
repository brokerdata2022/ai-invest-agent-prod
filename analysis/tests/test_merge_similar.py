"""
Тести build_prompt()/parse_response()/merge_stream() для
news_analysis/merge_similar.py — той самий підхід, що інші LLM-скрипти
(чисті функції без мережі, monkeypatch call_deepseek для наскрізного).
"""

import json

import llm_common
import pytest

from news_analysis.merge_similar import MergeResponseError, build_prompt, merge_stream, parse_response

ROWS = [
    {"id": 327, "asset_id": "xauusd", "summary": "Золото впало до понад 7-тижневого мінімуму, подешевшавши на 4%.",
     "direction": "down", "confidence": 0.8, "source_count": 7,
     "source_raw_news_ids": [1, 2, 3, 4, 5, 6, 7], "source_urls": [f"https://a.com/{i}" for i in range(7)]},
    {"id": 361, "asset_id": "xauusd", "summary": "Золото впало на 4% до 7-тижневого мінімуму через високі ціни на нафту.",
     "direction": "down", "confidence": 0.7, "source_count": 1,
     "source_raw_news_ids": [8], "source_urls": ["https://b.com/1"]},
    {"id": 403, "asset_id": None, "summary": "Золото впало до семитижневого мінімуму на тлі зростання цін на нафту.",
     "direction": "down", "confidence": 0.75, "source_count": 1,
     "source_raw_news_ids": [9], "source_urls": ["https://c.com/1"]},
]

VALID_RESPONSE = {"groups": [[1, 2, 3]]}


def test_build_prompt_numbers_rows_and_shows_asset():
    prompt = build_prompt(ROWS)
    assert "1. [xauusd] Золото впало до понад 7-тижневого" in prompt
    assert "3. Золото впало до семитижневого" in prompt  # asset_id=None — без тегу


def test_parse_response_valid_group():
    groups = parse_response(json.dumps(VALID_RESPONSE), n_rows=3)
    assert groups == [[1, 2, 3]]


def test_parse_response_accepts_empty_groups():
    assert parse_response(json.dumps({"groups": []}), n_rows=3) == []


def test_parse_response_rejects_invalid_json():
    with pytest.raises(MergeResponseError):
        parse_response("not json", n_rows=3)


def test_parse_response_rejects_missing_groups_field():
    with pytest.raises(MergeResponseError):
        parse_response(json.dumps({}), n_rows=3)


def test_parse_response_silently_drops_single_item_group():
    # LLM порушив системний промпт ("не треба групи з одного номера") —
    # не валимо всю відповідь через це, просто ігноруємо цю групу.
    assert parse_response(json.dumps({"groups": [[1]]}), n_rows=3) == []


def test_parse_response_rejects_out_of_range_index():
    with pytest.raises(MergeResponseError):
        parse_response(json.dumps({"groups": [[1, 99]]}), n_rows=3)


def test_parse_response_resolves_overlap_keeping_first_occurrence():
    # Регресія 2026-09-29 (живий баг): DeepSeek іноді повертає той
    # самий номер у двох групах — раніше це відкидало ВСЮ відповідь,
    # тепер лишає перше входження й прибирає з решти.
    groups = parse_response(json.dumps({"groups": [[1, 2], [2, 3]]}), n_rows=3)
    assert groups == [[1, 2]]  # [2, 3] → [3] → замала група, відкинуто


def test_parse_response_drops_group_left_with_single_item_after_overlap_resolution():
    groups = parse_response(json.dumps({"groups": [[1, 2, 3], [3]]}), n_rows=3)
    assert groups == [[1, 2, 3]]


def test_merge_stream_merges_and_returns_removed_count(monkeypatch):
    # Ключ ОБОВ'ЯЗКОВО теж підміняти, не лише call_deepseek:
    # llm_common._call_provider перевіряє змінну середовища ДО
    # виклику провайдера, тож без цього рядка підміна нижче
    # просто не досягається, і тест падає з LlmError. Без ключа в
    # середовищі — тобто саме на свіжому розгортанні, де .env ще
    # не заповнений (знайдено наскрізним тестом 2026-10-13).
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(llm_common, "call_deepseek", lambda *a, **k: json.dumps(VALID_RESPONSE))
    monkeypatch.setattr("news_analysis.merge_similar.fetch_mergeable", lambda conn, stream, window_hours: ROWS)
    monkeypatch.setattr("news_analysis.merge_similar.log_llm_call", lambda *a, **k: 42)

    merge_calls = []
    monkeypatch.setattr(
        "news_analysis.merge_similar.merge_consolidated_rows",
        lambda conn, group_rows: merge_calls.append(group_rows) or (len(group_rows) - 1),
    )

    removed = merge_stream(conn=None, stream="watchlist")

    assert removed == 2
    assert len(merge_calls) == 1
    assert {r["id"] for r in merge_calls[0]} == {327, 361, 403}


def test_merge_stream_skips_when_fewer_than_two_rows(monkeypatch):
    monkeypatch.setattr("news_analysis.merge_similar.fetch_mergeable", lambda conn, stream, window_hours: ROWS[:1])
    removed = merge_stream(conn=None, stream="watchlist")
    assert removed == 0


def test_merge_stream_noop_when_llm_finds_no_duplicates(monkeypatch):
    # Ключ ОБОВ'ЯЗКОВО теж підміняти, не лише call_deepseek:
    # llm_common._call_provider перевіряє змінну середовища ДО
    # виклику провайдера, тож без цього рядка підміна нижче
    # просто не досягається, і тест падає з LlmError. Без ключа в
    # середовищі — тобто саме на свіжому розгортанні, де .env ще
    # не заповнений (знайдено наскрізним тестом 2026-10-13).
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(llm_common, "call_deepseek", lambda *a, **k: json.dumps({"groups": []}))
    monkeypatch.setattr("news_analysis.merge_similar.fetch_mergeable", lambda conn, stream, window_hours: ROWS)
    monkeypatch.setattr("news_analysis.merge_similar.log_llm_call", lambda *a, **k: 42)

    removed = merge_stream(conn=None, stream="watchlist")
    assert removed == 0
