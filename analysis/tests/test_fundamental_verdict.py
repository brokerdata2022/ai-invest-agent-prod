"""
Двоетапний фундаментал (2026-10-24): аналіз кожного активу — DeepSeek,
один фінальний вердикт — Anthropic. Тут — вхід гейту змін і вердикт.
"""

from datetime import date, datetime, timezone

import pytest

import config
from fundamental.factors import AssetFactors, Factor
from fundamental.gate_inputs import asset_gate_inputs, stock_gate_inputs
from fundamental.verdict import (
    MAX_VERDICT_POINTS,
    build_verdict_prompt,
    parse_verdict_response,
)
from llm_common import SynthesisResponseError


def _daily(values, start_day=20):
    return [{"observed_at": f"2026-10-{start_day + i}", "value": v} for i, v in enumerate(values)]


def _factors():
    return AssetFactors(
        asset_id="wti_crude",
        label="Нафта WTI",
        factors=[
            Factor(key="price", label="Ціна", points=_daily(["70.1", "71.4"])),
            Factor(key="fred:DGS10", label="10Y", points=_daily(["4.21", "4.25"])),
            Factor(
                key="eia:crude_stocks", label="Запаси",
                points=[{"observed_at": "2026-10-09", "value": "425"},
                        {"observed_at": "2026-10-16", "value": "421"}],
                note="звірено",
            ),
            Factor(key="news", label="Новини", texts=["ОПЕК+ продовжив скорочення"]),
            Factor(key="eia:opec", label="Видобуток ОПЕК", missing_reason="немає даних"),
        ],
        gaps=("Запаси Китаю",),
    )


def test_price_and_daily_series_become_anchors():
    stable, anchors = asset_gate_inputs(_factors(), "manual")
    assert anchors["price"].tolerance == config.CHANGE_GATE_PRICE_PCT
    assert anchors["fred:DGS10"].tolerance == config.CHANGE_GATE_DAILY_FACTOR_PCT
    # Щоденні числа НЕ в дослівній частині — інакше гейт відкривався б щодня.
    keys_with_last = {f["key"] for f in stable["factors"] if "last" in f}
    assert keys_with_last == {"eia:crude_stocks"}


def test_weekly_series_new_point_changes_stable_part():
    before, _ = asset_gate_inputs(_factors(), "manual")
    factors = _factors()
    factors.factors[2].points.append({"observed_at": "2026-10-23", "value": "418"})
    after, _ = asset_gate_inputs(factors, "manual")
    assert before != after


def test_new_news_changes_stable_part():
    before, _ = asset_gate_inputs(_factors(), "manual")
    factors = _factors()
    factors.factors[3].texts.append("Нова історія")
    after, _ = asset_gate_inputs(factors, "manual")
    assert before != after


def test_stock_inputs_pe_is_anchor_reports_are_stable():
    series = {"Виручка": [{"observed_at": "2026-06-30", "value": "100"}]}
    stable, anchors = stock_gate_inputs(
        series, {"pe": "25.4", "revenue_growth": "0.1", "eps_growth": "0.2"},
        {"form": "10-Q", "filing_date": "2026-08-01"},
    )
    assert anchors["pe"].value == pytest.approx(25.4)
    assert stable["filing"] == ["10-Q", "2026-08-01"]


def _analysis(asset_id, created, direction="up"):
    return {
        "id": 1, "asset_id": asset_id, "company_name": asset_id.upper(), "asset_kind": "watchlist",
        "direction": direction, "confidence": 0.7, "summary": f"Підсумок {asset_id}",
        "strengths": ["за"], "risks": ["проти"], "created_at": created,
    }


def test_verdict_prompt_marks_unchanged_assets():
    today = date(2026, 10, 23)
    prompt = build_verdict_prompt(
        [
            _analysis("xauusd", datetime(2026, 10, 23, 11, tzinfo=timezone.utc)),
            _analysis("wti_crude", datetime(2026, 10, 20, 11, tzinfo=timezone.utc), "down"),
        ],
        today,
    )
    assert "XAUUSD (xauusd, актив списку) — вгору" in prompt
    assert "[без змін з 2026-10-20]" in prompt
    assert prompt.count("без змін") == 1
    assert "За: за" in prompt and "Проти: проти" in prompt


def test_parse_verdict_caps_points_and_requires_summary():
    raw = '{"summary": "Головне.", "key_points": %s, "conflicts": [], "watch": ["CPI"]}' % (
        str([f"п{i}" for i in range(10)]).replace("'", '"')
    )
    result = parse_verdict_response(raw)
    assert result.summary == "Головне."
    assert len(result.key_points) == MAX_VERDICT_POINTS
    assert result.watch == ["CPI"]
    with pytest.raises(SynthesisResponseError):
        parse_verdict_response('{"key_points": []}')
