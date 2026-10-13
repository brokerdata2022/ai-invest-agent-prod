"""
Тести build_prompt() (чиста функція) і synthesize_asset() з підміненим
call_deepseek — той самий підхід, що test_relevance_filter.py.

Розбір відповіді LLM і вибір провайдера тут НЕ тестуються — вони спільні
для всіх LLM-скриптів (llm_common.py) і покриті test_llm_common.py.
"""

import json
from decimal import Decimal

import pytest

import news_analysis.synthesize as synthesize_module
from llm_common import LlmResponse
from llm_common import SynthesisResponseError
from news_analysis.aggregate import AssetSignal
from news_analysis.prices import compute_pct_change
from news_analysis.synthesize import build_prompt, parse_price_news_response, synthesize_asset

SIGNAL = AssetSignal(
    asset_id="xauusd",
    cluster_count=3,
    direction_counts={"up": 2, "down": 0, "neutral": 1, "unclear": 0},
    net_lean=2,
    summaries=["Fed тримає ставки високими довше, ніж очікувалось.", "Попит на золото центробанків зростає."],
)

PRICE = compute_pct_change("xauusd", Decimal("4000"), "2026-09-20", Decimal("4200"), "2026-09-26")

VALID_RESPONSE = {
    "direction": "up",
    "confidence": 0.75,
    "summary": "Зростання ціни золота узгоджується з новинним сигналом про високі ставки.",
    "confirmation_factors": "Якщо ріст продовжиться без нових новин про ставки — це вже не просто реакція.",
    "reasoning": "Новини й рух ціни в одному напрямку, 3 незалежні джерела.",
}


def test_build_prompt_includes_core_fields():
    prompt = build_prompt("xauusd", SIGNAL, PRICE)
    assert "xauusd" in prompt
    assert "net_lean=+2" in prompt
    assert "5.00%" in prompt
    assert "Fed тримає ставки високими довше" in prompt


def test_build_prompt_omits_facts_section_when_no_summaries():
    signal = AssetSignal(
        asset_id="wti_crude", cluster_count=1,
        direction_counts={"up": 0, "down": 0, "neutral": 1, "unclear": 0},
        net_lean=0, summaries=[],
    )
    prompt = build_prompt("wti_crude", signal, PRICE)
    assert "Факти з новин:" not in prompt


def test_build_prompt_states_explicitly_when_no_news_at_all():
    # 2026-10-02, живий фідбек користувача: синтез тепер МОЖЕ
    # запускатись без жодної новини (тригер -- аномальний рух ціни,
    # prices.py:is_anomalous_move()) -- LLM має явно побачити "новин
    # немає", не вгадувати це з дампу нульового direction_counts.
    empty_signal = AssetSignal(
        asset_id="btc", cluster_count=0,
        direction_counts={"up": 0, "down": 0, "neutral": 0, "unclear": 0},
        net_lean=0, summaries=[],
    )
    prompt = build_prompt("btc", empty_signal, PRICE)
    assert "новин про цей актив не знайдено" in prompt.lower()
    assert "net_lean" not in prompt


def test_synthesize_asset_calls_llm_and_parses(monkeypatch):
    def fake_call_llm(prompt, system_prompt, purpose=None):
        return LlmResponse(text=json.dumps(VALID_RESPONSE), provider="deepseek", model="deepseek-chat")

    monkeypatch.setattr(synthesize_module, "call_llm", fake_call_llm)

    result, prompt, llm = synthesize_asset("xauusd", SIGNAL, PRICE)

    assert result.direction == "up"
    assert result.confirmation_factors == VALID_RESPONSE["confirmation_factors"]
    assert "xauusd" in prompt
    assert llm.text == json.dumps(VALID_RESPONSE)


def test_parse_price_news_response_requires_confirmation_factors():
    # 2026-10-02: confirmation_factors -- нове обов'язкове поле поруч
    # із direction/confidence/summary/reasoning (synthesize.py:
    # PRICE_NEWS_FIELDS), без нього гіпотеза тренд/корекція лишається
    # без того, що її підтвердить чи спростує.
    incomplete = {k: v for k, v in VALID_RESPONSE.items() if k != "confirmation_factors"}
    with pytest.raises(SynthesisResponseError):
        parse_price_news_response(json.dumps(incomplete))


def test_parse_price_news_response_returns_confirmation_factors():
    result = parse_price_news_response(json.dumps(VALID_RESPONSE))
    assert result.confirmation_factors == VALID_RESPONSE["confirmation_factors"]
