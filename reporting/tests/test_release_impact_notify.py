from datetime import date

from release_impact_notify import format_impact_message


def test_format_impact_message_header_and_summary():
    row = {
        "metric_id": "cpi",
        "observed_at": date(2026, 9, 30),
        "summary": "Вийшло вище прогнозу — інфляційний тиск сильніший, ніж очікувалось.",
        "asset_impacts": [],
    }
    text = format_impact_message(row)
    assert "🌐 Вплив релізу на ринок: CPI (інфляція, США) — 2026-09-30" in text
    assert "Вийшло вище прогнозу" in text


def test_format_impact_message_renders_each_impact_with_assets_and_explanation():
    row = {
        "metric_id": "cpi",
        "observed_at": date(2026, 9, 30),
        "summary": "Сюрприз вгору.",
        "asset_impacts": [
            {
                "category": "крипта",
                "assets": "BTC, ETH",
                "direction": "down",
                "explanation": "Довше утримання ставки тисне на risk-on активи.",
            },
            {
                "category": "золото",
                "assets": "XAU/USD",
                "direction": "up",
                "explanation": "Вищий ризик рецесії підтримує захисні активи.",
            },
        ],
    }
    text = format_impact_message(row)
    assert "🔴 <b>Крипта</b> (BTC, ETH)" in text
    assert "Довше утримання ставки" in text
    assert "🟢 <b>Золото</b> (XAU/USD)" in text
    assert "Вищий ризик рецесії" in text


def test_format_impact_message_omits_parens_when_assets_empty():
    row = {
        "metric_id": "cpi",
        "observed_at": date(2026, 9, 30),
        "summary": "Сюрприз вгору.",
        "asset_impacts": [
            {
                "category": "інший_актив",
                "assets": "",
                "direction": "unclear",
                "explanation": "Неоднозначний вплив на товарні ринки.",
            },
        ],
    }
    lines = format_impact_message(row).splitlines()
    assert "❓ <b>Інший актив</b>" in lines


def test_format_impact_message_falls_back_to_raw_metric_id_when_unknown():
    row = {
        "metric_id": "some_new_metric",
        "observed_at": date(2026, 9, 30),
        "summary": "...",
        "asset_impacts": [],
    }
    assert "some_new_metric" in format_impact_message(row)
