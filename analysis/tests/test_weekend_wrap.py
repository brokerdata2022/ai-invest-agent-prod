"""Підсумок вихідних (2026-10-24): промпт і розбір відповіді."""

import pytest

from llm_common import SynthesisResponseError
from news_analysis.weekend_wrap import MAX_POINTS, build_prompt, parse_response


def test_prompt_contains_stories_crypto_and_macro():
    prompt = build_prompt(
        stories=[{"summary": "Удар по танкеру", "source_count": 4, "direction": "up", "asset_id": None}],
        crypto=[{"label": "BTC", "pct_change": -3.2, "start_date": "2026-10-23", "end_date": "2026-10-25"}],
        macro={"10Y": {"latest_value": "4.25", "latest_date": "2026-10-23"}},
    )
    assert "[4 джерел, напрямок up] Удар по танкеру" in prompt
    assert "BTC: -3.20%" in prompt
    assert "10Y: 4.25" in prompt


def test_prompt_says_explicitly_when_nothing_happened():
    prompt = build_prompt([], [], {})
    assert "підтверджених історій немає" in prompt
    assert "даних немає" in prompt


def test_parse_response_validates_direction_and_caps_lists():
    result = parse_response(
        '{"summary": "Тихо.", "direction": "neutral", "confidence": 0.4, '
        '"gap_risks": ["a","b","c","d","e","f","g"], "crypto": "BTC стоїть", "watch_monday": []}'
    )
    assert result["direction"] == "neutral"
    assert len(result["gap_risks"]) == MAX_POINTS
    with pytest.raises(SynthesisResponseError):
        parse_response('{"summary": "x", "direction": "sideways"}')
