"""
Тести звірки джерел (`common/consensus.py`).

Усі числа нижче — РЕАЛЬНІ значення з `raw_observations` станом на
2026-10-12, не вигадані: саме на них шар і писався. Тест стереже
поведінку на живому кейсі, а не на синтетичному прикладі.
"""

from decimal import Decimal

import pytest

from common.consensus import (
    AGREE,
    DISAGREE,
    INSUFFICIENT,
    OUTLIER,
    SourceValue,
    reconcile,
)

TOL = Decimal("3")


def test_real_brent_case_names_fred_as_outlier():
    """Живий кейс, через який шар і з'явився: Brent за 2026-10-02.
    FRED (офіційне першоджерело!) розходився з рештою на 33%, а
    правильне значення 102.25 підтвердила РУЧНА звірка по двох сайтах
    (source=web_crosscheck). Офіційність джерела не корелює з
    правильністю — звідси медіана, а не "довіряти найофіційнішому"."""
    verdict = reconcile("brent_crude", [
        SourceValue("fred", Decimal("135.51")),
        SourceValue("tradingeconomics", Decimal("102.25")),
        SourceValue("web_crosscheck", Decimal("102.25")),
    ], TOL)

    assert verdict.status == OUTLIER
    assert verdict.outliers == ["fred"]
    assert verdict.consensus == Decimal("102.25")
    assert verdict.usable, "два згодні джерела дають придатне значення"
    assert verdict.max_spread_pct > Decimal("30")


def test_two_disagreeing_sources_give_no_winner():
    """WTI за 2026-10-02: FRED 97.89 проти TradingEconomics 91.11.
    Двоє не згодні — переможця визначити НІЧИМ, тож значення
    непридатне. Це свідомо суворіше за "візьмемо перше": тихо вибрати
    навмання тут гірше, ніж сказати "не знаю"."""
    verdict = reconcile("wti_crude", [
        SourceValue("fred", Decimal("97.89")),
        SourceValue("tradingeconomics", Decimal("91.11")),
    ], TOL)

    assert verdict.status == DISAGREE
    assert verdict.consensus is None
    assert not verdict.usable


def test_natural_spread_between_independent_sources_is_agreement():
    """Живий замір Yahoo↔TradingEconomics: кава 289.90/285.89 —
    розкид 1.40%, найгірший серед 6 активів. Це природна різниця
    контрактів/часу зрізу, а не помилка, і допуск 3% має її
    пропускати. Інакше шар кричав би щодня й знецінився (рівно те, що
    сталось із прапорцем "нестабільні ревізії": він висів на 8 із 11
    активів, бо для крипти щогодинне оновлення читалось як ревізія)."""
    verdict = reconcile("coffee", [
        SourceValue("yahoo_chart", Decimal("289.90")),
        SourceValue("tradingeconomics", Decimal("285.89")),
    ], TOL)

    assert verdict.status == AGREE
    assert verdict.usable
    assert verdict.max_spread_pct < TOL


def test_single_source_is_insufficient_but_still_returns_value():
    """Одне джерело — звіряти ні з чим. Значення віддаємо (краще мати
    його, ніж нічого), але статус це ЯВНО фіксує, щоб "незвірене" не
    виглядало як "перевірене"."""
    verdict = reconcile("usdjpy", [
        SourceValue("yahoo_chart", Decimal("158.66")),
    ], TOL)

    assert verdict.status == INSUFFICIENT
    assert verdict.consensus == Decimal("158.66")
    assert not verdict.usable


def test_no_sources_at_all():
    verdict = reconcile("coffee", [], TOL)
    assert verdict.status == INSUFFICIENT
    assert verdict.consensus is None


def test_median_resists_single_liar_among_three():
    """Суть вибору медіани: один брехун не зсуває вердикт, навіть
    якщо він у рази."""
    verdict = reconcile("xauusd", [
        SourceValue("a", Decimal("4190")),
        SourceValue("b", Decimal("4200")),
        SourceValue("c", Decimal("41900")),
    ], TOL)

    assert verdict.status == OUTLIER
    assert verdict.outliers == ["c"]
    assert verdict.consensus == Decimal("4195")


def test_crypto_tolerance_allows_wider_spread():
    """Для крипти допуск ширший (5%): різні біржі, пари USDT vs USD,
    цілодобова торгівля без спільного закриття. 4% розкиду між
    Binance і Yahoo — норма, не викид."""
    verdict = reconcile("btc", [
        SourceValue("binance", Decimal("83000")),
        SourceValue("yahoo_chart", Decimal("82437")),
    ], Decimal("5"))

    assert verdict.status == AGREE
    assert verdict.usable


@pytest.mark.parametrize("values", [
    [SourceValue("a", Decimal("0")), SourceValue("b", Decimal("0"))],
])
def test_zero_values_do_not_divide_by_zero(values):
    verdict = reconcile("x", values, TOL)
    assert verdict.status == AGREE
    assert verdict.consensus == Decimal("0")


# --- абсолютний допуск (2026-10-20, факторні ряди) ---------------------

def test_abs_tolerance_rescues_near_zero_values():
    """Мінімальна температура біля нуля: 0.4 проти 1.1°C — це 47%
    "розбіжності" від медіани, але 0.35°C по суті. Без абсолютного
    допуску прогноз заморозків щодня читався б як розбіжність джерел."""
    values = [SourceValue("open_meteo", Decimal("0.4")), SourceValue("met_norway", Decimal("1.1"))]

    assert reconcile("brazil_coffee_min_temp_7d", values, Decimal("0")).status == DISAGREE
    verdict = reconcile("brazil_coffee_min_temp_7d", values, Decimal("0"), Decimal("2.5"))
    assert verdict.status == AGREE


def test_abs_tolerance_does_not_hide_a_real_gap():
    """Живий замір 2026-10-20 — EIA −8557 проти TradingEconomics −8560
    тис. бар. (округлення до 0.01 млн) — у межах; а різниця на мільйон
    барелів — уже ні."""
    ok = reconcile("us_crude_stocks", [
        SourceValue("eia", Decimal("-8557")), SourceValue("tradingeconomics", Decimal("-8560")),
    ], Decimal("1"), Decimal("15"))
    assert ok.status == AGREE

    bad = reconcile("us_crude_stocks", [
        SourceValue("eia", Decimal("-8557")), SourceValue("tradingeconomics", Decimal("-7560")),
    ], Decimal("1"), Decimal("15"))
    assert bad.status == DISAGREE


def test_without_abs_tolerance_behaviour_is_unchanged():
    """Наявні виклики (watchlist, макро, котирування) абсолютного допуску
    не передають — для них нічого не змінилось."""
    verdict = reconcile("x", [SourceValue("a", Decimal("100")), SourceValue("b", Decimal("110"))], TOL)
    assert verdict.status == DISAGREE
