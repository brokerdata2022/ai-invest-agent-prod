"""
Тести build_prompt()/synthesize_comparison() для
analysis/expectations/synthesize.py — той самий підхід, що
test_synthesize.py (monkeypatch call_deepseek, без мережі/БД).

Розбір відповіді LLM і вибір провайдера — спільні (llm_common.py),
покриті test_llm_common.py.
"""

import json

import expectations.synthesize as expectations_synthesize_module
from llm_common import LlmResponse
from expectations.synthesize import (
    build_prompt,
    parse_expectation_synthesis_response,
    synthesize_comparison,
)

COMPARISON = {
    "comparison_id": 1,
    "source": "fred",
    "metric_id": "cpi",
    "observed_at": "2026-09-30",
    "actual_value": 0.5,
    "expected_value_raw": "0.4%",
    "expected_value_parsed": 0.4,
    "surprise": 0.1,
    "surprise_pct": 25.0,
    "comparison_method": "mom_pct",
    "impact_level": "high",
}

VALID_RESPONSE = {
    "direction": "up",
    "confidence": 0.7,
    "summary": "Вийшло 0.5%, очікувалось 0.4% — інфляція вища за прогноз.",
    "reasoning": "Сюрприз +25% від очікування, це суттєве відхилення для high-impact релізу.",
}


def test_build_prompt_includes_core_fields():
    prompt = build_prompt(COMPARISON)
    assert "cpi" in prompt
    assert "0.5" in prompt
    assert "0.4" in prompt
    assert "high" in prompt
    assert "+25.00%" in prompt


def test_build_prompt_omits_pct_line_when_none():
    comparison = dict(COMPARISON, surprise_pct=None)
    prompt = build_prompt(comparison)
    assert "% від очікування" not in prompt


def test_synthesize_comparison_calls_llm_and_parses(monkeypatch):
    def fake_call_llm(prompt, system_prompt, purpose=None):
        return LlmResponse(text=json.dumps(VALID_RESPONSE), provider="deepseek", model="deepseek-chat")

    monkeypatch.setattr(expectations_synthesize_module, "call_llm", fake_call_llm)

    result, prompt, llm = synthesize_comparison(COMPARISON)

    assert result.direction == "up"
    assert result.impacts == []  # VALID_RESPONSE без impacts — список порожній, не падає
    assert "cpi" in prompt
    assert llm.text == json.dumps(VALID_RESPONSE)


def test_parse_keeps_only_well_formed_impacts():
    """2026-10-02, живий фідбек користувача: комплексний розбір впливу
    на ставку/економіку/валюту/акції/крипту/золото. Некоректний елемент
    масиву (бракує explanation) не повинен валити весь синтез —
    основний висновок (summary/direction) лишається корисним без нього."""
    response = dict(
        VALID_RESPONSE,
        impacts=[
            {
                "category": "крипта",
                "assets": "BTC, ETH",
                "direction": "down",
                "explanation": "Вищий за прогноз CPI підвищує шанс довшого утримання ставки — тиск на risk-on активи.",
            },
            {"category": "золото", "direction": "up"},  # без explanation — пропускається
            {"category": "", "direction": "up", "explanation": "без категорії"},  # пропускається
        ],
    )

    result = parse_expectation_synthesis_response(json.dumps(response))

    assert len(result.impacts) == 1
    assert result.impacts[0]["category"] == "крипта"
    assert result.impacts[0]["direction"] == "down"


def test_parse_defaults_impacts_to_empty_list_when_absent():
    result = parse_expectation_synthesis_response(json.dumps(VALID_RESPONSE))
    assert result.impacts == []
