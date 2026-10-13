"""
Ринковий контекст прогнозу (`forecasting/market_context.py`) — чисті
функції, без БД і мережі.

Причина існування модуля — прямий фідбек користувача 2026-10-11:
"дохідності Treasury це важливий показник, він впливає на зміну ставки
і є випереджаючим індикатором". Доти вони були ціллю точкового
прогнозу (де переваги немає) і НЕ подавались у прогнози, де несуть
сигнал.
"""

from datetime import date

import pytest

import config
from forecasting.market_context import (
    build_snapshot,
    change_over,
    format_snapshot,
    value_as_of,
)

# Крива, що інвертується: 2Y росте швидше за 10Y.
SHORT = [
    (date(2026, 8, 1), 3.50),
    (date(2026, 9, 1), 3.90),
    (date(2026, 9, 25), 4.20),
    (date(2026, 10, 5), 4.77),
]
LONG = [
    (date(2026, 8, 1), 4.10),
    (date(2026, 9, 1), 4.40),
    (date(2026, 9, 25), 4.60),
    (date(2026, 10, 5), 4.50),
]


# ============================================================
# Заборона заглядати в майбутнє
# ============================================================


def test_value_as_of_ignores_later_points():
    """Головна гарантія: у бектесті контекст рахується станом на
    перевірену точку. Якби сюди протікали пізніші дані, прогноз на
    2015 рік спирався б на дохідності 2026-го, і вся виміряна точність
    була б фікцією."""
    assert value_as_of(SHORT, date(2026, 9, 1)) == 3.90
    assert value_as_of(SHORT, date(2026, 9, 24)) == 3.90  # 25-го ще не було
    assert value_as_of(SHORT, date(2026, 10, 5)) == 4.77


def test_value_as_of_returns_none_before_series_starts():
    assert value_as_of(SHORT, date(2026, 7, 1)) is None


def test_context_never_uses_future_points():
    """Те саме для всього знімка одразу: станом на 1 вересня спред
    мусить читатись із вересневих значень, не з жовтневих."""
    snapshot = build_snapshot(SHORT, LONG, date(2026, 9, 1))
    assert snapshot.short_value == 3.90
    assert snapshot.long_value == 4.40
    assert snapshot.spread == pytest.approx(0.5)

    later = build_snapshot(SHORT, LONG, date(2026, 10, 5))
    assert later.spread < 0  # інверсія настала пізніше


# ============================================================
# Зміни за вікно
# ============================================================


def test_change_over_uses_nearest_earlier_point():
    # За 30 днів до 2026-10-05 -> 2026-09-05, остання наявна точка
    # 2026-09-01 (3.90); поточна 4.77.
    assert change_over(SHORT, date(2026, 10, 5), 30) == pytest.approx(4.77 - 3.90)


def test_change_over_none_without_earlier_point():
    assert change_over(SHORT, date(2026, 8, 2), 30) is None


# ============================================================
# Форматування для промпта
# ============================================================


def test_format_marks_inversion_explicitly():
    """Знак спреду і є сигналом, тож він називається словом — модель не
    має виводити його сама з двох чисел."""
    text = format_snapshot(build_snapshot(SHORT, LONG, date(2026, 10, 5)))
    assert "ІНВЕРСІЯ" in text
    assert "-0.27" in text


def test_format_marks_normal_curve():
    text = format_snapshot(build_snapshot(SHORT, LONG, date(2026, 9, 1)))
    assert "нормальна крива" in text
    assert "ІНВЕРСІЯ" not in text


def test_format_shows_both_levels_and_changes():
    text = format_snapshot(build_snapshot(SHORT, LONG, date(2026, 10, 5)))
    assert "2Y: 4.77%" in text
    assert "10Y: 4.5%" in text
    assert "за тиждень" in text and "за місяць" in text


def test_format_returns_none_without_any_level():
    """Порожній блок у промпті лише заплутав би модель — краще не
    додавати нічого."""
    assert format_snapshot(build_snapshot([], [], date(2026, 10, 5))) is None


def test_format_survives_one_missing_series():
    """Якщо один ряд ще не зібраний — показуємо "н/д", а не падаємо й
    не підмінюємо нулем (нуль тут читався б як 0% дохідності)."""
    text = format_snapshot(build_snapshot(SHORT, [], date(2026, 10, 5)))
    assert "н/д" in text
    assert "спред" not in text


# ============================================================
# Перелік показників
# ============================================================


def test_context_targets_are_rate_sensitive_us_metrics():
    """Контекст подається лише туди, де крива впливає ПРЯМО. Японія/
    єврозона свідомо не включені: там свої криві, і американська була б
    хибним сигналом."""
    assert "fed_funds_rate" in config.FORECAST_YIELD_CONTEXT_METRICS
    assert "cpi" in config.FORECAST_YIELD_CONTEXT_METRICS
    for foreign in ("japan_cpi", "eurozone_hicp", "japan_policy_rate"):
        assert foreign not in config.FORECAST_YIELD_CONTEXT_METRICS


def test_yield_series_are_not_their_own_context():
    """Подавати 2Y у промпт прогнозу 2Y безсенсово — це той самий ряд,
    який модель і так бачить."""
    assert "treasury_2y" not in config.FORECAST_YIELD_CONTEXT_METRICS
    assert "treasury_10y" not in config.FORECAST_YIELD_CONTEXT_METRICS


# ============================================================
# Вердикт мусить читатись із ТІЄЇ САМОЇ конфігурації, що в продакшені
# ============================================================
# Живий дефект 2026-10-11: `mortgage_rate_30y` на промпті `levels` дає
# -3.8% (ШІ гірший), на `levels+yields` — +16.0% (ШІ кращий). Продакшен
# подає цьому показнику контекст дохідностей, тож читати вердикт без
# контексту означало б вимкнути ШІ на підставі виміру конфігурації,
# якої в продакшені немає.


def test_production_variant_includes_yields_for_context_metrics():
    from forecasting.llm_forecast import PROMPT_VARIANT, production_prompt_variant

    assert production_prompt_variant("mortgage_rate_30y") == f"{PROMPT_VARIANT}+yields"
    assert production_prompt_variant("cpi") == f"{PROMPT_VARIANT}+yields"


def test_production_variant_is_plain_for_metrics_without_context():
    from forecasting.llm_forecast import PROMPT_VARIANT, production_prompt_variant

    assert production_prompt_variant("japan_cpi") == PROMPT_VARIANT
    assert production_prompt_variant("treasury_2y") == PROMPT_VARIANT


def test_every_context_metric_has_a_production_variant_with_yields():
    """Інакше додавання показника в перелік контексту тихо лишило б
    його читати вердикт не своєї конфігурації."""
    from forecasting.llm_forecast import production_prompt_variant

    for metric_id in config.FORECAST_YIELD_CONTEXT_METRICS:
        assert production_prompt_variant(metric_id).endswith("+yields")
