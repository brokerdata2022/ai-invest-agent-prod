"""
Тести build_prompt()/synthesize_outlook() для
analysis/calendar_outlook/synthesize.py — той самий підхід, що
test_synthesize_expectations.py (monkeypatch call_deepseek, без
мережі/БД). Розбір відповіді й вибір провайдера — спільні
(llm_common.py), покриті test_llm_common.py.
"""

import json

import calendar_outlook.synthesize as calendar_synthesize_module
from llm_common import LlmResponse
from calendar_outlook.synthesize import build_prompt, synthesize_outlook

ENTRY = {
    "id": 1, "source": "fred", "metric_id": "cpi",
    "scheduled_at": "2026-10-06 12:30", "impact_level": "high",
    "expected_value": "0.4%",
}

VALID_RESPONSE = {
    "direction": "unclear",
    "confidence": 0.5,
    "summary": "CPI — high-impact, здатен рухати ринок.",
    "reasoning": "Єдиний high-impact реліз на цей день.",
}


def test_build_prompt_no_entries_mentions_no_releases():
    prompt = build_prompt([], scope="day")
    assert "немає" in prompt


def test_build_prompt_includes_metric_time_and_impact():
    prompt = build_prompt([ENTRY], scope="day")
    assert "cpi" in prompt
    assert "2026-10-06 12:30" in prompt
    assert "high" in prompt
    assert "0.4%" in prompt


def test_build_prompt_omits_forecast_when_absent():
    entry = dict(ENTRY, expected_value=None)
    prompt = build_prompt([entry], scope="day")
    assert "ринковий прогноз" not in prompt


def test_build_prompt_week_scope_mentions_week():
    prompt = build_prompt([ENTRY], scope="week")
    assert "тиждень" in prompt


def test_synthesize_outlook_calls_llm_and_parses(monkeypatch):
    def fake_call_llm(prompt, system_prompt, purpose=None):
        return LlmResponse(text=json.dumps(VALID_RESPONSE), provider="deepseek", model="deepseek-chat")

    monkeypatch.setattr(calendar_synthesize_module, "call_llm", fake_call_llm)

    result, prompt, llm = synthesize_outlook([ENTRY], scope="day")

    assert result.direction == "unclear"
    assert "cpi" in prompt
    assert llm.text == json.dumps(VALID_RESPONSE)
