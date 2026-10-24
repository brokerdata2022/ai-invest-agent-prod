"""
Входи фундаментального аналізу для ГЕЙТУ ЗМІН (`analysis/change_gate.py`,
2026-10-24) — що в аналізі має збігатися дослівно, а що порівнюється з
допуском. Чиста частина, без SQL.

Межа між двома частинами проходить по ЧАСТОТІ ряду, а не по його
назві: ціна й щоденні ряди (дохідності, курс долара, погода) рухаються
щодня трохи і йдуть у `anchors` з порогом; рідші ряди (запаси щотижня,
CPI щомісяця, видобуток) — у `stable`, бо їхня нова точка і є нові дані.
"""

from datetime import date

import config
from change_gate import Anchor

PRICE_KEYS_PREFIX = ("asset:",)


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_daily(points: list[dict]) -> bool:
    if len(points) < 2:
        return False
    try:
        last = date.fromisoformat(str(points[-1]["observed_at"])[:10])
        prev = date.fromisoformat(str(points[-2]["observed_at"])[:10])
    except ValueError:
        return False
    return (last - prev).days <= config.CHANGE_GATE_DAILY_GAP_DAYS


def asset_gate_inputs(asset_factors, checklist_origin: str) -> tuple[dict, dict[str, Anchor]]:
    """(stable, anchors) для аналізу активу watchlist за його чинниками."""
    stable_factors, anchors = [], {}
    for factor in asset_factors.factors:
        entry = {"key": factor.key, "note": factor.note, "missing": factor.missing_reason}
        if factor.texts:
            entry["texts"] = list(factor.texts)
        if factor.points:
            last = factor.points[-1]
            value = _as_float(last["value"])
            is_price = factor.key == "price" or factor.key.startswith(PRICE_KEYS_PREFIX)
            if value is not None and (is_price or _is_daily(factor.points)):
                tolerance = (
                    config.CHANGE_GATE_PRICE_PCT if is_price else config.CHANGE_GATE_DAILY_FACTOR_PCT
                )
                anchors[factor.key] = Anchor(value, tolerance, relative=True)
            else:
                entry["last"] = [str(last["observed_at"]), str(last["value"])]
        stable_factors.append(entry)
    stable = {
        "factors": stable_factors,
        "gaps": list(asset_factors.gaps),
        "checklist": checklist_origin,
    }
    return stable, anchors


def stock_gate_inputs(series: dict, valuation: dict, filing_insight) -> tuple[dict, dict[str, Anchor]]:
    """(stable, anchors) для аналізу звітності акції. Звітність і тези
    звіту — дослівно (міняються раз на квартал); P/E рухається з ціною
    щодня — з допуском, як ціна."""
    stable = {
        "series": {
            label: [[str(p["observed_at"]), str(p["value"])] for p in points]
            for label, points in series.items()
        },
        "growth": [str(valuation.get("revenue_growth")), str(valuation.get("eps_growth"))],
        "filing": (
            [str(filing_insight.get("form")), str(filing_insight.get("filing_date"))]
            if filing_insight else None
        ),
    }
    anchors = {}
    pe = _as_float(valuation.get("pe"))
    if pe is not None:
        anchors["pe"] = Anchor(pe, config.CHANGE_GATE_PRICE_PCT, relative=True)
    return stable, anchors
