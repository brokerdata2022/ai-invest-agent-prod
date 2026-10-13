"""
Тести build_prompt()/parse_response()/consolidate_stream() для
news_analysis/consolidate.py — 2026-09-28, архітектура змінена:
механічна кластеризація (aggregate.py:cluster_articles(), без LLM)
ПЕРЕД LLM-викликом; LLM отримує представників кластерів, не сирі
статті; source_indices у відповіді LLM тепер референсять НОМЕРИ
КЛАСТЕРІВ, не статей.
"""

import json
from datetime import datetime, timezone

import llm_common
import pytest

from news_analysis.aggregate import NewsCluster, cluster_articles
from news_analysis.consolidate import (
    ConsolidationResponseError,
    build_prompt,
    consolidate_stream,
    parse_response,
)


def _cluster(**overrides) -> NewsCluster:
    defaults = dict(
        representative_title="Bitcoin перевищив 84 000 доларів",
        source_count=2,
        asset_ids=[],
        dominant_direction="unclear",
        direction_counts={"up": 0, "down": 0, "neutral": 0, "unclear": 2},
        summaries=[],
        urls=["https://abcnews.al/a", "https://syri-vision.tv/a"],
        earliest_published_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
        latest_published_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
        raw_news_ids=[1, 2],
    )
    defaults.update(overrides)
    return NewsCluster(**defaults)


VALID_RESPONSE = {
    "items": [
        {
            "asset_id": "btc",
            "summary": "Bitcoin перевищив 84 000 доларів, наблизившись до найвищого рівня за 8 місяців.",
            "direction": "up",
            "confidence": 0.8,
            "reasoning": "Підтверджено кількома незалежними джерелами.",
            "source_indices": [1],
        }
    ]
}


# ---------- build_prompt() — на прямо сконструйованих кластерах, без
# залежності від внутрішнього сортування cluster_articles() ----------

def test_build_prompt_shows_source_count_and_numbers_clusters():
    clusters = [
        _cluster(representative_title="Bitcoin перевищив 84 000 доларів", source_count=2),
        _cluster(representative_title="Aldi to invest £900m in UK", source_count=1, raw_news_ids=[3]),
    ]
    articles_by_id = {
        1: {"id": 1, "title": "Bitcoin перевищив 84 000 доларів", "raw_payload": {"description": "BTC опис"}},
        3: {"id": 3, "title": "Aldi to invest £900m in UK", "raw_payload": None},
    }
    prompt = build_prompt(clusters, articles_by_id, tracked_assets=["btc", "xauusd"])

    assert "1. [2 джерел] Bitcoin перевищив 84 000 доларів" in prompt
    assert "2. [1 джерел] Aldi to invest £900m in UK" in prompt
    assert "Опис: BTC опис" in prompt
    assert "Відстежувані активи: btc, xauusd" in prompt


def test_build_prompt_omits_tracked_assets_when_none():
    prompt = build_prompt([_cluster()], articles_by_id={}, tracked_assets=None)
    assert "Відстежувані активи" not in prompt


# ---------- parse_response() — той самий контракт, індекси тепер
# валідуються проти n_clusters ----------

def test_parse_response_valid():
    items = parse_response(json.dumps(VALID_RESPONSE), n_clusters=1)
    assert len(items) == 1
    assert items[0]["source_indices"] == [1]
    assert items[0]["confidence"] == 0.8


def test_parse_response_rejects_invalid_json():
    with pytest.raises(ConsolidationResponseError):
        parse_response("not json", n_clusters=1)


def test_parse_response_rejects_missing_items_field():
    with pytest.raises(ConsolidationResponseError):
        parse_response(json.dumps({}), n_clusters=1)


def test_parse_response_rejects_out_of_range_index():
    data = {"items": [dict(VALID_RESPONSE["items"][0], source_indices=[1, 99])]}
    with pytest.raises(ConsolidationResponseError):
        parse_response(json.dumps(data), n_clusters=1)


def test_parse_response_rejects_empty_source_indices():
    data = {"items": [dict(VALID_RESPONSE["items"][0], source_indices=[])]}
    with pytest.raises(ConsolidationResponseError):
        parse_response(json.dumps(data), n_clusters=1)


def test_parse_response_accepts_empty_items_list():
    # Усе відкинуто як шум — нормальний результат.
    assert parse_response(json.dumps({"items": []}), n_clusters=1) == []


# ---------- consolidate_stream() — наскрізно: кластеризація → LLM →
# збереження, з реальним cluster_articles() (не мокнутим) ----------

ARTICLES_SAME_STORY = [
    {
        "id": 1, "stream": "watchlist", "title": "Bitcoin mbi 84 mijë dollarë",
        "url": "https://abcnews.al/a", "published_at": datetime(2026, 9, 27, tzinfo=timezone.utc),
        "raw_payload": {"description": "Bitcoin ka kaluar nivelin e 84,000 dollarëve."},
    },
    {
        "id": 2, "stream": "watchlist", "title": "Bitcoin mbi 84 mijë dollarë - TV SYRI",
        "url": "https://syri-vision.tv/a", "published_at": datetime(2026, 9, 27, tzinfo=timezone.utc),
        "raw_payload": None,
    },
]


class _FakeConn:
    """Мінімальний фейковий conn — consolidate_stream() лише передає
    його далі в log_llm_call()/save_consolidated_item(), які тут теж
    підмінені monkeypatch, тому реальний cursor/commit не потрібен."""


def test_mechanical_clustering_merges_same_story_before_llm_sees_it():
    """Регресія 2026-09-28: раніше LLM мусив сам здогадатись, що 2
    албанські статті — одна подія. Тепер це вже зроблено ДО промпту."""
    clusters = cluster_articles([{**a, "raw_news_id": a["id"]} for a in ARTICLES_SAME_STORY])
    assert len(clusters) == 1
    assert clusters[0].source_count == 2


def test_consolidate_stream_uses_mechanical_source_count_not_llm_count(monkeypatch):
    # Ключ ОБОВ'ЯЗКОВО теж підміняти, не лише call_deepseek:
    # llm_common._call_provider перевіряє змінну середовища ДО
    # виклику провайдера, тож без цього рядка підміна нижче
    # просто не досягається, і тест падає з LlmError. Без ключа в
    # середовищі — тобто саме на свіжому розгортанні, де .env ще
    # не заповнений (знайдено наскрізним тестом 2026-10-13).
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(llm_common, "call_deepseek", lambda *a, **k: json.dumps(VALID_RESPONSE))

    saved_calls = []
    monkeypatch.setattr(
        "news_analysis.consolidate.save_consolidated_item",
        lambda conn, **kwargs: saved_calls.append(kwargs) or len(saved_calls),
    )
    monkeypatch.setattr("news_analysis.consolidate.log_llm_call", lambda *a, **k: 42)
    monkeypatch.setattr("news_analysis.consolidate.fetch_recent_summaries", lambda conn, stream: [])

    result = consolidate_stream(_FakeConn(), "watchlist", ARTICLES_SAME_STORY)

    assert len(result) == 1
    assert result[0]["source_count"] == 2  # механічний факт кластера, не LLM
    assert sorted(saved_calls[0]["source_raw_news_ids"]) == [1, 2]
    assert sorted(saved_calls[0]["source_urls"]) == ["https://abcnews.al/a", "https://syri-vision.tv/a"]
    assert saved_calls[0]["asset_id"] == "btc"


def test_consolidate_stream_skips_items_duplicating_recent_summaries(monkeypatch):
    # Ключ ОБОВ'ЯЗКОВО теж підміняти, не лише call_deepseek:
    # llm_common._call_provider перевіряє змінну середовища ДО
    # виклику провайдера, тож без цього рядка підміна нижче
    # просто не досягається, і тест падає з LlmError. Без ключа в
    # середовищі — тобто саме на свіжому розгортанні, де .env ще
    # не заповнений (знайдено наскрізним тестом 2026-10-13).
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.setattr(llm_common, "call_deepseek", lambda *a, **k: json.dumps(VALID_RESPONSE))
    monkeypatch.setattr("news_analysis.consolidate.log_llm_call", lambda *a, **k: 42)
    monkeypatch.setattr(
        "news_analysis.consolidate.fetch_recent_summaries",
        lambda conn, stream: [VALID_RESPONSE["items"][0]["summary"]],
    )

    saved_calls = []
    monkeypatch.setattr(
        "news_analysis.consolidate.save_consolidated_item",
        lambda conn, **kwargs: saved_calls.append(kwargs),
    )

    result = consolidate_stream(_FakeConn(), "watchlist", ARTICLES_SAME_STORY)

    assert result == []
    assert saved_calls == []
