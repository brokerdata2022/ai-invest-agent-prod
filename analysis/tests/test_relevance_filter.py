"""
Тести build_prompt()/parse_response() (чисті функції, без мережі) і
analyze_article() з підміненим call_deepseek — те саме, чого вимагає
.claude/skills/add-data-source п.3 для звичайних адаптерів.
"""

import json

import news_analysis.relevance_filter as relevance_filter_module
from llm_common import LlmResponse

import pytest

from news_analysis.relevance_filter import (
    DeepSeekResponseError,
    extract_description,
    analyze_article,
    build_prompt,
    parse_response,
)

ARTICLE = {
    "title": "Nvidia beats earnings expectations on AI demand",
    "url": "https://example-wire.com/markets/nvidia-earnings-beat",
    "published_at": "2026-09-25T09:15:00+00:00",
}

VALID_RESPONSE = {
    "is_relevant": True,
    "asset_id": "NVDA",
    "direction": "up",
    "confidence": 0.8,
    "summary": "Nvidia перевершила очікування по прибутку завдяки попиту на AI-чипи.",
    "reasoning": "Сильні квартальні результати зазвичай підтримують ціну акції короткостроково.",
}


def test_build_prompt_includes_core_fields():
    prompt = build_prompt(ARTICLE, stream="watchlist", tracked_assets=["NVDA", "AAPL"])
    assert "Nvidia beats earnings" in prompt
    assert "watchlist" in prompt
    assert "NVDA, AAPL" in prompt


def test_build_prompt_without_tracked_assets():
    prompt = build_prompt(ARTICLE, stream="geopolitical")
    assert "Відстежувані активи" not in prompt


def test_build_prompt_includes_description_when_present():
    # Regression: без опису DeepSeek оцінює релевантність з самого
    # заголовка (docs/decisions.md, 2026-09-26) -- забагато шуму.
    article = dict(ARTICLE, raw_payload={"description": "Q3 revenue beat estimates by 12%."})
    prompt = build_prompt(article, stream="watchlist")
    assert "Опис: Q3 revenue beat estimates by 12%." in prompt


def test_build_prompt_omits_description_line_when_absent():
    prompt = build_prompt(ARTICLE, stream="watchlist")
    assert "Опис:" not in prompt


def test_build_prompt_omits_description_line_when_gdelt_style_payload():
    # GDELT raw_payload не має "description" взагалі -- .get() безпечний.
    article = dict(ARTICLE, raw_payload={"domain": "example.com", "language": "English"})
    prompt = build_prompt(article, stream="watchlist")
    assert "Опис:" not in prompt


def testextract_description_handles_missing_or_non_dict_payload():
    assert extract_description({}) is None
    assert extract_description({"raw_payload": None}) is None
    assert extract_description({"raw_payload": "not a dict"}) is None
    assert extract_description({"raw_payload": {"description": None}}) is None
    assert extract_description({"raw_payload": {"description": "   "}}) is None
    assert extract_description({"raw_payload": {"description": " text "}}) == "text"


def test_parse_response_valid():
    result = parse_response(json.dumps(VALID_RESPONSE))
    assert result.is_relevant is True
    assert result.asset_id == "NVDA"
    assert result.direction == "up"
    assert result.confidence == 0.8


def test_parse_response_null_asset_id():
    data = dict(VALID_RESPONSE, asset_id=None)
    result = parse_response(json.dumps(data))
    assert result.asset_id is None


def test_parse_response_rejects_invalid_json():
    with pytest.raises(DeepSeekResponseError):
        parse_response("not json")


def test_parse_response_rejects_missing_fields():
    data = dict(VALID_RESPONSE)
    del data["direction"]
    with pytest.raises(DeepSeekResponseError):
        parse_response(json.dumps(data))


def test_parse_response_rejects_unknown_direction():
    data = dict(VALID_RESPONSE, direction="sideways")
    with pytest.raises(DeepSeekResponseError):
        parse_response(json.dumps(data))


def test_parse_response_rejects_confidence_out_of_range():
    data = dict(VALID_RESPONSE, confidence=1.5)
    with pytest.raises(DeepSeekResponseError):
        parse_response(json.dumps(data))


def test_analyze_article_calls_llm_and_parses(monkeypatch):
    """2026-10-05: фільтр більше не кличе DeepSeek напряму — усі
    виклики йдуть через `llm_common.call_llm`, щоб провайдер вибирався
    в ОДНОМУ місці (`config.py:LLM_ROUTES`) і аудит-лог знав
    фактичну модель."""
    captured = {}

    def fake_call_llm(prompt, system_prompt, purpose=None):
        captured["prompt"] = prompt
        captured["system_prompt"] = system_prompt
        captured["purpose"] = purpose
        return LlmResponse(text=json.dumps(VALID_RESPONSE), provider="deepseek", model="deepseek-chat")

    monkeypatch.setattr(relevance_filter_module, "call_llm", fake_call_llm)

    result, prompt, llm = analyze_article(
        ARTICLE, stream="watchlist", tracked_assets=["NVDA"]
    )

    assert result.asset_id == "NVDA"
    assert prompt == captured["prompt"]
    assert llm.text == json.dumps(VALID_RESPONSE)
    # Призначення обов'язкове — саме воно й обирає маршрут.
    assert captured["purpose"] == "news_relevance_filter"
    assert captured["system_prompt"] is not None
