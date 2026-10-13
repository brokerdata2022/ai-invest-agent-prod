"""
Тести форматування торгового списку. Окрема увага — тому, через що
перший варіант повідомлення переписувався (живий фідбек користувача
2026-10-04: "не читабильний а технічний"): скори не наголо, metric_id
перекладені, той самий реліз не повторюється, "суперечливо" з
розшифровкою, речення не обриваються на півслові.
"""

from decimal import Decimal

from trading_list_notify import (
    describe_conflict,
    explain,
    format_message,
    metric_label,
    strength_word,
    trim,
)

ROW = {
    "id": 1,
    "asset_id": "xagusd",
    "label": "Срібло (XAG/USD)",
    "kind": "commodity",
    "direction": "up",
    "score": Decimal("0.63"),
    "reasons": [
        {"kind": "release_upcoming", "direction": "neutral",
         "metric_id": "mortgage_rate_30y", "detail": "", "when": "08.10 12:30"},
        {"kind": "release_done", "direction": "up",
         "metric_id": "unemployment_rate", "detail": "Слабший ринок праці", "when": ""},
        {"kind": "trend", "direction": "up", "metric_id": "",
         "detail": "+5.20% коротке / +12.10% довге — узгоджені", "when": ""},
    ],
}


# --- дрібні хелпери --------------------------------------------------


def test_metric_label_translates_known_metric():
    assert metric_label("mortgage_rate_30y") != "mortgage_rate_30y"
    assert "іпотек" in metric_label("mortgage_rate_30y").lower() or "Mortgage" in metric_label("mortgage_rate_30y")


def test_metric_label_passes_unknown_through():
    assert metric_label("made_up_metric") == "made_up_metric"


def test_strength_word_three_levels():
    assert strength_word(Decimal("0.9")) == "сильний"
    assert strength_word(Decimal("0.6")) == "помірний"
    assert strength_word(Decimal("0.2")) == "слабкий"


def test_trim_cuts_on_word_boundary():
    """Живий дефект: речення обривались посередині слова."""
    text = "Ціна срібла впала на шість відсотків за тиждень через зміцнення долара"
    out = trim(text, 30)
    assert out.endswith("…")
    assert not out[:-1].endswith(" ")
    # Остання частина — ціле слово, не огризок.
    assert out[:-1].split()[-1] in text.split()


def test_trim_leaves_short_text_untouched():
    assert trim("коротко", 50) == "коротко"


def test_trim_collapses_whitespace():
    assert trim("два\n\nрядки   поспіль", 100) == "два рядки поспіль"


# --- суперечність ----------------------------------------------------


def test_describe_conflict_names_both_sides():
    """Без цього позначка "суперечливо" була марною."""
    reasons = [
        {"kind": "trend", "direction": "up"},
        {"kind": "news", "direction": "down"},
    ]
    text = describe_conflict(reasons)
    assert "тренд ціни" in text
    assert "новини" in text
    assert "вгору" in text and "вниз" in text


def test_describe_conflict_empty_when_no_disagreement():
    reasons = [{"kind": "trend", "direction": "up"}, {"kind": "news", "direction": "up"}]
    assert describe_conflict(reasons) == ""


def test_describe_conflict_does_not_repeat_same_source():
    reasons = [
        {"kind": "release_done", "direction": "up"},
        {"kind": "release_done", "direction": "up"},
        {"kind": "news", "direction": "down"},
    ]
    text = describe_conflict(reasons)
    assert text.count("вийшлі дані") == 1


# --- explain ---------------------------------------------------------


def test_explain_groups_upcoming_releases():
    lines = explain(ROW["reasons"])
    upcoming = [line for line in lines if line.startswith("📅")]
    assert len(upcoming) == 1
    assert "08.10 12:30" in upcoming[0]
    assert "mortgage_rate_30y" not in upcoming[0], "metric_id має бути перекладений"


def test_explain_does_not_repeat_same_release():
    """Живий дефект: один реліз давав три рядки (по категорії впливу)."""
    reasons = [
        {"kind": "release_done", "direction": "up", "metric_id": "unemployment_rate",
         "detail": "ставка", "when": ""},
        {"kind": "release_done", "direction": "neutral", "metric_id": "unemployment_rate",
         "detail": "економіка", "when": ""},
        {"kind": "release_done", "direction": "neutral", "metric_id": "unemployment_rate",
         "detail": "акції", "when": ""},
    ]
    lines = explain(reasons)
    done = [line for line in lines if line.startswith("📊")]
    assert len(done) == 1
    assert done[0].count(metric_label("unemployment_rate")) == 1


def test_explain_includes_trend_line():
    lines = explain(ROW["reasons"])
    assert any(line.startswith("📈") for line in lines)


def test_explain_takes_only_one_news_item():
    reasons = [
        {"kind": "news", "direction": "up", "metric_id": "", "detail": f"новина {i}", "when": "3"}
        for i in range(4)
    ]
    lines = explain(reasons)
    assert len([line for line in lines if line.startswith("📰")]) == 1


def test_explain_empty_reasons_gives_no_lines():
    assert explain([]) == []


# --- повне повідомлення ---------------------------------------------


def test_message_uses_human_label_not_asset_id():
    text = format_message([ROW])
    assert "Срібло (XAG/USD)" in text
    assert "XAGUSD" not in text


def test_message_has_no_raw_scores():
    """Головна причина переписування: внутрішні числа в чаті не
    потрібні — вони лишаються в БД для калібрування."""
    text = format_message([ROW])
    assert "0.63" not in text
    assert "каталізатор" not in text
    assert "якість" not in text


def test_message_shows_qualitative_strength():
    text = format_message([ROW])
    assert "помірний" in text


def test_message_direction_in_words():
    text = format_message([ROW])
    assert "сигнал вгору" in text


def test_message_conflict_explained_not_just_flagged():
    row = dict(
        ROW, direction="conflicting",
        reasons=[
            {"kind": "trend", "direction": "up", "metric_id": "", "detail": "+5%", "when": ""},
            {"kind": "news", "direction": "down", "metric_id": "", "detail": "негатив", "when": "4"},
        ],
    )
    text = format_message([row])
    assert "сигнали розходяться" in text
    assert "тренд ціни" in text and "новини" in text


def test_message_counts_assets_in_header():
    text = format_message([ROW, dict(ROW, id=2, asset_id="btc", label="BTC/USDT")])
    assert "Активи для розгляду — 2" in text


def test_message_falls_back_to_asset_id_without_label():
    row = dict(ROW, label=None)
    text = format_message([row])
    assert "xagusd" in text


def test_message_escapes_html_in_reason_text():
    row = dict(
        ROW,
        reasons=[{"kind": "news", "direction": "up", "metric_id": "",
                  "detail": "зростання <5% & стабільне", "when": "3"}],
    )
    text = format_message([row])
    assert "&lt;5% &amp; стабільне" in text


def test_message_ends_with_non_advice_disclaimer():
    text = format_message([ROW])
    assert "не вказівка діяти" in text


# --- макро-фон і специфіка активу ------------------------------------


def test_macro_background_is_union_not_intersection():
    """Перша спроба брала ПЕРЕТИН спільних причин і не спрацювала:
    набори справді різні (в акцій є claims, у крипти немає). Макро за
    визначенням фон, тож беремо обʼєднання."""
    from trading_list_notify import macro_background

    rows = [
        {"reasons": [{"kind": "release_upcoming", "metric_id": "cpi",
                      "direction": "neutral", "detail": "", "when": "08.10"}]},
        {"reasons": [{"kind": "release_upcoming", "metric_id": "retail_sales",
                      "direction": "neutral", "detail": "", "when": "09.10"}]},
    ]
    metrics = {r["metric_id"] for r in macro_background(rows)}
    assert metrics == {"cpi", "retail_sales"}


def test_macro_background_lists_each_metric_once():
    from trading_list_notify import macro_background

    reason = {"kind": "release_done", "metric_id": "cpi", "direction": "up",
              "detail": "", "when": ""}
    rows = [{"reasons": [reason]}, {"reasons": [dict(reason, direction="down")]}]
    assert len(macro_background(rows)) == 1


def test_macro_background_drops_direction():
    """Та сама подія може бути 'вгору' для золота й 'вниз' для валюти —
    у шапці лишається сама подія, без напрямку."""
    from trading_list_notify import macro_background

    rows = [{"reasons": [{"kind": "release_done", "metric_id": "cpi",
                          "direction": "up", "detail": "", "when": ""}]}]
    assert macro_background(rows)[0]["direction"] == "neutral"


def test_asset_specific_drops_background_kinds():
    """Рівно те, що робило 6 акцій дослівно однаковими: заплановані
    релізи, прогноз і вийшлі релізи без напрямку — це фон, не
    специфіка активу."""
    from trading_list_notify import asset_specific

    reasons = [
        {"kind": "release_upcoming", "metric_id": "cpi", "direction": "neutral"},
        {"kind": "forecast", "metric_id": "cpi", "direction": "neutral"},
        {"kind": "release_done", "metric_id": "cpi", "direction": "neutral"},
        {"kind": "release_done", "metric_id": "unemployment_rate", "direction": "up"},
        {"kind": "news", "metric_id": "", "direction": "down"},
        {"kind": "trend", "metric_id": "", "direction": "up"},
    ]
    kinds = [r["kind"] for r in asset_specific(reasons)]
    assert kinds == ["release_done", "news", "trend"]


def test_message_shows_macro_background_once():
    rows = [
        dict(ROW, id=1, asset_id="a", label="A"),
        dict(ROW, id=2, asset_id="b", label="B"),
    ]
    text = format_message(rows)
    assert text.count("📌 Макро-фон") == 1
    # Запланований реліз згадується лише у фоні, не в кожному рядку.
    assert text.count("📅 Попереду") == 1


def test_message_says_plainly_when_asset_has_no_own_catalyst():
    row = dict(ROW, reasons=[
        {"kind": "release_upcoming", "metric_id": "cpi",
         "direction": "neutral", "detail": "", "when": "08.10"},
    ])
    text = format_message([row])
    assert "власних новин немає" in text


def test_conflict_names_metrics_not_generic_words():
    """Було: "вийшлі дані — вгору, а вийшлі дані — вниз"."""
    reasons = [
        {"kind": "release_done", "metric_id": "unemployment_rate", "direction": "up"},
        {"kind": "release_done", "metric_id": "eurozone_hicp", "direction": "down"},
    ]
    text = describe_conflict(reasons)
    assert metric_label("unemployment_rate") in text
    assert metric_label("eurozone_hicp") in text
    assert "вийшлі дані" not in text
