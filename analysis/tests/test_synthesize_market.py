"""
Тести build_prompt() (чиста функція) і synthesize_market() з підміненим
call_deepseek — той самий підхід, що test_synthesize.py.

Розбір відповіді LLM і вибір провайдера — спільні (llm_common.py),
покриті test_llm_common.py.
"""

import json
from datetime import datetime, timezone

import news_analysis.synthesize_market as synthesize_market_module
from llm_common import LlmResponse
from news_analysis.aggregate import NewsCluster
from news_analysis.synthesize_market import (
    build_prompt,
    synthesize_market as run_synthesize_market,
)

CLUSTER = NewsCluster(
    representative_title="Fed signals rates could stay higher for longer",
    source_count=5,
    asset_ids=[],
    dominant_direction="down",
    direction_counts={"up": 0, "down": 4, "neutral": 1, "unclear": 0},
    summaries=["Fed officials hint rate cuts may be delayed into next year."],
    urls=["https://example.com/a"],
    earliest_published_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
    latest_published_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
)

MACRO = {
    "US 10Y Treasury Yield": {
        "latest_value": "4.85", "latest_date": "2026-09-26",
        "previous_value": "4.70", "previous_date": "2026-09-19",
    },
}

VALID_RESPONSE = {
    "direction": "down",
    "confidence": 0.7,
    "summary": "Risk-off: дохідності зростають разом із ведмежим новинним сигналом про ставки.",
    "reasoning": "Новини про 'вище довше' узгоджуються зі зростанням 10Y дохідності.",
}


def test_build_prompt_includes_macro_and_clusters():
    prompt = build_prompt([CLUSTER], MACRO)
    assert "US 10Y Treasury Yield" in prompt
    assert "4.85" in prompt
    assert "попереднє 4.70" in prompt
    assert "Fed signals rates could stay higher for longer" in prompt
    assert "5 джерел" in prompt


def test_build_prompt_handles_missing_macro_context():
    prompt = build_prompt([CLUSTER], {})
    assert "немає даних" in prompt


def test_synthesize_market_calls_llm_and_parses(monkeypatch):
    def fake_call_llm(prompt, system_prompt, purpose=None):
        return LlmResponse(text=json.dumps(VALID_RESPONSE), provider="deepseek", model="deepseek-chat")

    monkeypatch.setattr(synthesize_market_module, "call_llm", fake_call_llm)

    result, prompt, llm = run_synthesize_market([CLUSTER], MACRO)

    assert result.direction == "down"
    assert "Fed signals rates" in prompt
    assert llm.text == json.dumps(VALID_RESPONSE)


# --- сесійний контекст (спек 2026-09-28, реалізовано 2026-10-04) ------


def test_session_context_goes_into_prompt():
    """Навіщо сесія в ПРОМПТІ, а не лише в розкладі: без неї три
    прогони за добу дали б три майже однакові висновки з тих самих
    даних."""
    from news_analysis.synthesize_market import SESSIONS, build_prompt

    for session, context in SESSIONS.items():
        prompt = build_prompt([], {}, session=session)
        assert context in prompt, f"{session}: контекст сесії не потрапив у промпт"


def test_session_prompts_differ_between_sessions():
    from news_analysis.synthesize_market import build_prompt

    asia = build_prompt([], {}, session="asia")
    us = build_prompt([], {}, session="us")
    assert asia != us


def test_unknown_session_falls_back_without_context():
    """Невідома сесія не має валити прогін — просто без контексту."""
    from news_analysis.synthesize_market import build_prompt

    prompt = build_prompt([], {}, session="daily")
    assert "Макро-контекст:" in prompt


def test_three_sessions_registered():
    """Рівно три сесії зі спеку — Азія/Європа/США."""
    from news_analysis.synthesize_market import SESSIONS

    assert set(SESSIONS) == {"asia", "europe", "us"}
