"""
Тести чистих функцій `analysis/fundamental/fundamental_llm.py` —
промпт і розбір відповіді. Без мережі/БД (той самий підхід, що
test_llm_forecast.py).
"""

import json

import pytest
from llm_common import SynthesisResponseError

from fundamental.fundamental_llm import (
    MAX_POINTS,
    build_prompt,
    parse_fundamental_response,
)

SERIES = {
    "Виручка": [
        {"observed_at": "2025-10-31", "value": 7_800_000_000},
        {"observed_at": "2026-01-31", "value": 8_100_000_000},
        {"observed_at": "2026-04-30", "value": 8_400_000_000},
    ],
    "EPS (розмитий)": [
        {"observed_at": "2026-01-31", "value": "0.38"},
        {"observed_at": "2026-04-30", "value": "0.44"},
    ],
}

VALID = {
    "direction": "up",
    "confidence": 0.7,
    "summary": "Виручка зростає третій квартал поспіль.",
    "reasoning": "Зростання 7.8 → 8.4 млрд при зростанні EPS.",
    "strengths": ["Виручка зростає", "EPS зростає швидше за виручку"],
    "risks": ["Зобовʼязання лише за один період"],
}


# --- build_prompt ----------------------------------------------------


def test_prompt_includes_company_and_ticker():
    prompt = build_prompt("HPE", "Hewlett Packard Enterprise", SERIES)
    assert "Hewlett Packard Enterprise" in prompt
    assert "HPE" in prompt


def test_prompt_falls_back_to_ticker_without_name():
    assert "(HPE)" in build_prompt("HPE", None, SERIES)


def test_prompt_formats_large_numbers_readably():
    """LLM краще тлумачить порядок, коли він названий словом, а не
    11 цифрами."""
    prompt = build_prompt("HPE", None, SERIES)
    assert "7.80 млрд" in prompt
    assert "7800000000" not in prompt


def test_prompt_includes_every_series():
    prompt = build_prompt("HPE", None, SERIES)
    assert "Виручка" in prompt
    assert "EPS (розмитий)" in prompt


def test_prompt_includes_valuation_when_available():
    prompt = build_prompt("HPE", None, SERIES, {
        "pe": "18.5", "revenue_growth": "0.12", "eps_growth": "0.21",
    })
    assert "P/E: 18.50" in prompt
    assert "+12.0%" in prompt
    assert "+21.0%" in prompt


def test_prompt_omits_valuation_block_when_absent():
    prompt = build_prompt("HPE", None, SERIES, None)
    assert "P/E" not in prompt


def test_prompt_skips_empty_series():
    prompt = build_prompt("HPE", None, {"Виручка": [], "EPS (розмитий)": SERIES["EPS (розмитий)"]})
    assert "Виручка —" not in prompt
    assert "EPS (розмитий) —" in prompt


# --- parse_fundamental_response --------------------------------------


def test_parse_valid_response():
    result = parse_fundamental_response(json.dumps(VALID))
    assert result.direction == "up"
    assert result.confidence == pytest.approx(0.7)
    assert len(result.strengths) == 2
    assert len(result.risks) == 1


def test_parse_rejects_missing_core_field():
    payload = dict(VALID)
    del payload["summary"]
    with pytest.raises(SynthesisResponseError, match="бракує полів"):
        parse_fundamental_response(json.dumps(payload))


def test_parse_rejects_unknown_direction():
    with pytest.raises(SynthesisResponseError, match="direction"):
        parse_fundamental_response(json.dumps(dict(VALID, direction="покращується")))


def test_parse_accepts_missing_strengths_and_risks():
    """Основний висновок лишається корисним і без списків — валити
    через них увесь аналіз було б надто строго (той самий принцип, що
    expectations/synthesize.py:_parse_impacts)."""
    payload = {k: v for k, v in VALID.items() if k not in ("strengths", "risks")}
    result = parse_fundamental_response(json.dumps(payload))
    assert result.strengths == []
    assert result.risks == []


def test_parse_skips_malformed_points_without_failing():
    payload = dict(VALID, strengths=["норм", "", None, 42, "ще норм"])
    result = parse_fundamental_response(json.dumps(payload))
    assert result.strengths == ["норм", "ще норм"]


def test_parse_accepts_dict_shaped_points():
    """LLM іноді віддає [{"point": "..."}] замість ["..."] — дешевше
    прийняти, ніж відкинути корисний вміст."""
    payload = dict(VALID, risks=[{"point": "високий борг"}])
    assert parse_fundamental_response(json.dumps(payload)).risks == ["високий борг"]


def test_parse_caps_number_of_points():
    payload = dict(VALID, strengths=[f"пункт {i}" for i in range(20)])
    assert len(parse_fundamental_response(json.dumps(payload)).strengths) == MAX_POINTS


def test_parse_rejects_non_json():
    with pytest.raises(SynthesisResponseError):
        parse_fundamental_response("компанія виглядає добре")
