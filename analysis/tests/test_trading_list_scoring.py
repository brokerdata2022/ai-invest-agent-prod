"""
Тести чистих функцій `analysis/trading_list/scoring.py` — скоринг
торгового списку. Без БД і без мережі (той самий підхід, що
test_trend.py / test_indicators.py).

Окрема увага — властивостям, які тримають САМ ДИЗАЙН
(docs/trading-list.md), а не лише арифметику:
- watchlist-бонус НЕ протягує актив без каталізатора через поріг
  (інакше список вироджується у /notify_watchlist);
- поріг застосовується ДО резервування слотів;
- суперечність компонентів не розвʼязується більшістю голосів;
- макро приглушене для акцій (не різнить 215 тикерів між собою).
"""

from decimal import Decimal

import pytest

import config
from trading_list.categories import (
    CATEGORY_TO_WATCHLIST_ASSETS,
    IMPACT_CATEGORIES,
    category_affects_stocks,
    watchlist_assets_for_category,
)
from trading_list.scoring import (
    CatalystHit,
    ScoreBreakdown,
    catalyst_score,
    combine,
    pct_change,
    quality_score,
    resolve_direction,
    score_asset,
    select_final,
    trend_score,
)

D = Decimal


def _rising(n: int, step: str = "1") -> list[Decimal]:
    """Рівномірно зростаючий ряд від 100."""
    return [D("100") + D(step) * i for i in range(n)]


def _flat(n: int) -> list[Decimal]:
    return [D("100")] * n


# --- конфіг і мапа категорій -----------------------------------------


def test_component_weights_sum_to_one():
    """Інакше фінальний скор виходить за [0,1] і поріг MIN_SCORE
    перестає читатись як відсоток."""
    total = config.W_CATALYST + config.W_TREND + config.W_QUALITY
    assert total == Decimal("1")


def test_category_map_covers_only_known_categories():
    assert set(CATEGORY_TO_WATCHLIST_ASSETS) <= set(IMPACT_CATEGORIES)


def test_unknown_category_returns_empty_not_raises():
    """Перелік категорій може розширитись у промпті expectations/
    раніше, ніж тут — це не причина валити весь прогін списку."""
    assert watchlist_assets_for_category("нова_категорія") == ()


def test_gold_category_maps_to_metals():
    assert set(watchlist_assets_for_category("золото")) == {"xauusd", "xagusd"}


def test_currency_category_does_not_map_to_commodities():
    assets = watchlist_assets_for_category("валюта")
    assert "wti_crude" not in assets
    assert "eurusd" in assets


def test_only_broad_categories_affect_stocks():
    assert category_affects_stocks("ставка")
    assert not category_affects_stocks("золото")


# --- pct_change ------------------------------------------------------


def test_pct_change_basic():
    assert pct_change([D("100"), D("110")], 1) == D("10")


def test_pct_change_none_when_history_too_short():
    assert pct_change([D("100")], 5) is None


def test_pct_change_none_on_zero_base():
    assert pct_change([D("0"), D("5")], 1) is None


def test_pct_change_handles_negative_base_via_abs():
    """Ряд може йти в мінус (спред 10Y-2Y тощо) — знак бази не має
    перевертати знак %-зміни."""
    assert pct_change([D("-10"), D("-5")], 1) == D("50")


# --- trend_score -----------------------------------------------------


def test_trend_zero_when_history_too_short():
    score, direction, detail = trend_score(_rising(config.TREND_MIN_POINTS - 1))
    assert score == 0
    assert direction == "unclear"
    assert "недосить" in detail


def test_trend_zero_on_sideways():
    score, direction, detail = trend_score(_flat(40))
    assert score == 0
    assert direction == "neutral"
    assert "боковик" in detail


def test_trend_up_on_rising_series():
    score, direction, _ = trend_score(_rising(70))
    assert score > 0
    assert direction == "up"


def test_trend_down_on_falling_series():
    values = list(reversed(_rising(70)))
    score, direction, _ = trend_score(values)
    assert score > 0
    assert direction == "down"


def test_trend_saturates_and_never_exceeds_one():
    """Актив, що злетів у рази, не має автоматично займати весь топ."""
    explosive = [D("1") * (2 ** i) for i in range(30)]
    score, _, _ = trend_score(explosive)
    assert score == Decimal("1")


def test_trend_halved_when_windows_disagree():
    """Рух проти ширшого тренду менш надійний для свінгу, але не
    нульовий — може бути початком розвороту."""
    # Довге вікно вниз, коротке вгору: 61 точка падіння, потім різке
    # зростання на останніх 15. Довжини підібрані так, щоб довге вікно
    # було ЯВНО відʼємним (не рівно нуль) — інакше тест проходив би
    # через випадковість, а не через логіку розбіжності.
    falling = [D("200") - D("1") * i for i in range(61)]
    rebound = [falling[-1] + D("2") * i for i in range(1, 16)]
    score, direction, detail = trend_score(falling + rebound)
    assert direction == "up"
    assert "РОЗБІЖНІ" in detail
    assert 0 < score <= Decimal("0.5")


# --- catalyst_score --------------------------------------------------


def _release_high(text="CPI сьогодні"):
    return CatalystHit("release", config.CATALYST_WEIGHT_RELEASE_HIGH, "neutral", text)


def _news(text="3 джерела"):
    return CatalystHit("news", config.CATALYST_WEIGHT_NEWS, "down", text)


def test_catalyst_zero_without_hits():
    score, summary = catalyst_score([])
    assert score == 0
    assert summary == ""


def test_catalyst_full_on_release_plus_news():
    """Визначення "повного" каталізатора: високоімпактний реліз +
    значима новина = 1.0."""
    score, _ = catalyst_score([_release_high(), _news()])
    assert score == Decimal("1")


def test_catalyst_summary_joins_reasons():
    _, summary = catalyst_score([_release_high("реліз CPI"), _news("4 джерела")])
    assert "реліз CPI" in summary
    assert "4 джерела" in summary


def test_catalyst_macro_damped_for_every_asset_class():
    """ПЕРЕПИСАНО 2026-10-13. Раніше тест закріплював протилежне —
    що макро приглушене ЛИШЕ для акцій. Живий вивід 12 жовтня
    показав, чого це коштувало: `catalyst_score` = 0.647 ідентично
    для btc/eth/sol/eurusd/xauusd, 2 унікальні значення на 10
    активів, тобто "фільтр за каталізатором" не фільтрував нічого, а
    ранжування зводилось до тренду. Макро не різнить ні 215 тикерів,
    ні 11 watchlist-активів — приглушення мусить бути однакове."""
    as_stock, _ = catalyst_score([_release_high()], is_stock=True)
    as_other, _ = catalyst_score([_release_high()], is_stock=False)
    assert as_stock == as_other, "макро мусить приглушуватись однаково для всіх класів"


def test_own_news_outweighs_broad_macro():
    """Центральна властивість усього фільтра: прицільна подія
    (новина саме про цей актив) мусить важити БІЛЬШЕ за глобальний
    макрорелізу, інакше в топі стоять активи, про які нічого не
    відбувається. До 2026-10-13 було навпаки: макро 0.59 проти
    новини 0.41."""
    macro, _ = catalyst_score([_release_high()])
    own, _ = catalyst_score([_news()])
    assert own > macro
    assert own / macro > Decimal("2"), "прицільне мусить переважати широке в рази"


def test_catalyst_news_not_damped_for_stocks():
    """Новина про КОНКРЕТНИЙ тикер — саме той різнитель, що працює для
    акцій, тож приглушувати її не можна."""
    plain, _ = catalyst_score([_news()], is_stock=False)
    as_stock, _ = catalyst_score([_news()], is_stock=True)
    assert plain == as_stock


# --- quality_score ---------------------------------------------------


def test_quality_neutral_without_fundamentals():
    """Крипта/форекс/товари фундаменталу не мають — 0.5, не 0: нуль був
    би штрафом за відсутність даних."""
    assert quality_score(None, None) == Decimal("0.5")


def test_quality_relative_to_best_in_run():
    assert quality_score(D("5"), D("10")) == Decimal("0.5")


def test_quality_best_asset_gets_one():
    assert quality_score(D("10"), D("10")) == Decimal("1")


def test_quality_neutral_when_best_is_zero():
    assert quality_score(D("0"), D("0")) == Decimal("0.5")


# --- resolve_direction -----------------------------------------------


def test_direction_up_when_all_agree():
    assert resolve_direction(["up", "up", "neutral"]) == "up"


def test_direction_conflicting_not_majority_vote():
    """Два 'up' проти одного 'down' — це СУПЕРЕЧНІСТЬ, не 'up':
    вгадування більшістю приховало б корисний сигнал."""
    assert resolve_direction(["up", "up", "down"]) == "conflicting"


def test_direction_neutral_when_only_neutral():
    assert resolve_direction(["neutral", "neutral"]) == "neutral"


def test_direction_unclear_when_nothing_meaningful():
    assert resolve_direction(["unclear"]) == "unclear"
    assert resolve_direction([]) == "unclear"


# --- combine: головна антивироджувальна властивість ------------------


def test_watchlist_bonus_does_not_push_empty_asset_over_threshold():
    """КЛЮЧОВА властивість дизайну: watchlist-актив без каталізатора й
    без тренду НЕ потрапляє в список лише через бонус. Інакше список
    вироджується у /notify_watchlist, який уже існує."""
    total = combine(
        catalyst=Decimal("0"), trend=Decimal("0"),
        quality=Decimal("0.5"), is_watchlist=True,
    )
    assert total < config.MIN_SCORE


def test_watchlist_bonus_does_help_when_there_is_a_reason():
    without = combine(Decimal("0.5"), Decimal("0.5"), Decimal("0.5"), is_watchlist=False)
    with_bonus = combine(Decimal("0.5"), Decimal("0.5"), Decimal("0.5"), is_watchlist=True)
    assert with_bonus > without
    assert with_bonus - without == config.WATCHLIST_SCORE_BONUS


def test_combine_never_exceeds_one():
    assert combine(Decimal("1"), Decimal("1"), Decimal("1"), is_watchlist=True) == Decimal("1")


def test_combine_all_zero_is_zero():
    assert combine(Decimal("0"), Decimal("0"), Decimal("0")) == Decimal("0")


# --- score_asset -----------------------------------------------------


def test_score_asset_collects_breakdown_and_reasons():
    result = score_asset(
        values=_rising(70),
        hits=[_release_high("реліз CPI за 2 дні")],
        composite=D("8"), best_composite=D("10"),
    )
    assert isinstance(result, ScoreBreakdown)
    assert result.catalyst > 0
    assert result.trend > 0
    assert result.quality == Decimal("0.8")
    assert "реліз CPI за 2 дні" in result.catalyst_summary
    assert result.reasons


def test_score_asset_marks_conflict_between_trend_and_news():
    """Тренд вгору, новина вниз — саме той випадок, який має бути
    видний користувачу, а не згладжений."""
    result = score_asset(values=_rising(70), hits=[_news("негатив, 5 джерел")])
    assert result.direction == "conflicting"


def test_score_asset_uses_extra_direction_from_crypto_setup():
    """Крипта приносить готовий напрямок із власного сетапу
    (LONG → up) — перераховувати його тут нема сенсу."""
    result = score_asset(values=_flat(40), hits=[], extra_direction="down")
    assert result.direction == "down"


# --- select_final ----------------------------------------------------


def _bd(total: str) -> ScoreBreakdown:
    return ScoreBreakdown(
        total=Decimal(total), catalyst=Decimal("0.5"), trend=Decimal("0.5"),
        quality=Decimal("0.5"), direction="up",
    )


def test_select_final_drops_below_threshold():
    below = str(config.MIN_SCORE - Decimal("0.01"))
    scored = [("AAA", _bd("0.9"), False), ("BBB", _bd(below), False)]
    final = select_final(scored)
    assert [a for a, _, _ in final] == ["AAA"]


def test_select_final_threshold_applies_before_reserved_slots():
    """Порядок критичний: watchlist-актив, що НЕ пройшов поріг, не
    займає зарезервований слот лише тому, що він у watchlist — слот
    краще лишити порожнім."""
    below = str(config.MIN_SCORE - Decimal("0.01"))
    scored = [("gold", _bd(below), True), ("AAA", _bd("0.9"), False)]
    final = select_final(scored)
    assert [a for a, _, _ in final] == ["AAA"]


def test_select_final_reserves_slots_for_watchlist_over_higher_scores():
    """Watchlist зі скором нижче за акції все одно потрапляє —
    зарезервовані слоти (той самий механізм, що WATCHLIST_MIN_SLOTS
    для новин)."""
    scored = [(f"S{i}", _bd("0.95"), False) for i in range(config.MAX_ITEMS)]
    scored.append(("gold", _bd("0.40"), True))

    final = select_final(scored)
    names = [a for a, _, _ in final]

    assert "gold" in names
    assert len(final) == config.MAX_ITEMS


def test_select_final_caps_list_length():
    scored = [(f"S{i}", _bd("0.9"), False) for i in range(config.MAX_ITEMS * 3)]
    assert len(select_final(scored)) == config.MAX_ITEMS


def test_select_final_sorted_by_score_desc():
    scored = [("A", _bd("0.5"), False), ("B", _bd("0.9"), False), ("C", _bd("0.7"), False)]
    assert [a for a, _, _ in select_final(scored)] == ["B", "C", "A"]


def test_select_final_empty_input():
    assert select_final([]) == []


def test_select_final_all_below_threshold_returns_empty():
    """Тихий день — порожній список, а не заповнений шумом."""
    below = str(config.MIN_SCORE - Decimal("0.01"))
    scored = [("A", _bd(below), False), ("gold", _bd(below), True)]
    assert select_final(scored) == []


def test_select_final_does_not_duplicate_watchlist_asset():
    """Watchlist-актив із високим скором проходить і як
    зарезервований, і як топовий — не має зʼявитись двічі."""
    scored = [("gold", _bd("0.99"), True), ("AAA", _bd("0.80"), False)]
    final = select_final(scored)
    names = [a for a, _, _ in final]
    assert names.count("gold") == 1


@pytest.mark.parametrize("reserved", [0, 1, 5])
def test_select_final_respects_reserved_slot_setting(monkeypatch, reserved):
    monkeypatch.setattr(config, "WATCHLIST_RESERVED_SLOTS", reserved)
    scored = [(f"S{i}", _bd("0.95"), False) for i in range(config.MAX_ITEMS)]
    scored += [(f"w{i}", _bd("0.40"), True) for i in range(5)]

    final = select_final(scored)
    watchlist_in_final = [a for a, _, is_w in final if is_w]

    assert len(watchlist_in_final) == min(reserved, 5)
    assert len(final) == config.MAX_ITEMS


# --- живі баги, знайдені прогоном 2026-10-04 -------------------------


def test_catalyst_counts_only_strongest_hits():
    """Жива причина: проста сума ваг давала компонент 1.00 у половини
    активів — він насичувався й перестав РІЗНИТИ кандидатів. Тепер
    зараховуються лише CATALYST_MAX_COUNTED_HITS найсильніших."""
    many = [_news(f"новина {i}") for i in range(6)]
    score, _ = catalyst_score(many)

    # Знаменник той самий, що в scoring.py (з 2026-10-13 містить
    # MACRO_DAMPING — інакше стеля "повного каталізатора" стала б
    # недосяжною, див. докстрінг catalyst_score).
    from trading_list.categories import MACRO_DAMPING

    expected = (
        config.CATALYST_WEIGHT_NEWS * config.CATALYST_MAX_COUNTED_HITS
        / (
            config.CATALYST_WEIGHT_RELEASE_HIGH * Decimal(str(MACRO_DAMPING))
            + config.CATALYST_WEIGHT_NEWS
        )
    )
    assert score == min(expected, Decimal("1"))


def test_catalyst_extra_hits_still_listed_in_summary():
    """Обмеження діє на СКОР, не на текст — для людини зайві причини
    корисні."""
    many = [_news(f"новина {i}") for i in range(5)]
    _, summary = catalyst_score(many)
    assert summary.count("новина") == 5


def test_catalyst_takes_strongest_not_first():
    """Порядок хітів не має впливати на скор — беруться найсильніші."""
    weak_first = [
        CatalystHit("release", config.CATALYST_WEIGHT_RELEASE_LOW),
        CatalystHit("release", config.CATALYST_WEIGHT_RELEASE_HIGH),
        CatalystHit("news", config.CATALYST_WEIGHT_NEWS),
    ]
    strong_first = list(reversed(weak_first))
    assert catalyst_score(weak_first)[0] == catalyst_score(strong_first)[0]


def test_native_category_direction_only_for_own_asset():
    """Жива причина: усі 10 активів виходили `conflicting`, бо
    "ставка down" зараховувалась як голос ПРО СОЛАНУ — а це причина,
    не напрямок солани."""
    from trading_list.categories import is_native_category

    assert is_native_category("sol", "крипта")
    assert not is_native_category("sol", "ставка")
    assert is_native_category("xauusd", "золото")
    assert not is_native_category("xauusd", "економіка")


def test_native_category_for_stocks_is_equities():
    from trading_list.categories import is_native_category

    assert is_native_category("", "акції", is_stock=True)
    assert not is_native_category("", "ставка", is_stock=True)


def test_direction_neutral_hits_do_not_create_false_conflict():
    """Після фікса: хіти з чужих категорій мають direction='neutral',
    тож не створюють суперечності з трендом."""
    hits = [
        CatalystHit("release", config.CATALYST_WEIGHT_RELEASE_MEDIUM, "neutral", "ставка down"),
        CatalystHit("release", config.CATALYST_WEIGHT_RELEASE_MEDIUM, "up", "крипта up"),
    ]
    result = score_asset(values=_rising(70), hits=hits)
    assert result.direction == "up"


def test_genuine_conflict_between_releases_still_reported():
    """А справжня суперечність (два релізи розходяться у ВЛАСНІЙ
    категорії активу) лишається видимою — живий кейс 2026-10-04:
    eurozone_hicp казав 'акції down', а nonfarm_payrolls 'акції up'."""
    hits = [
        CatalystHit("release", config.CATALYST_WEIGHT_RELEASE_MEDIUM, "down", "hicp: акції down"),
        CatalystHit("release", config.CATALYST_WEIGHT_RELEASE_MEDIUM, "up", "nfp: акції up"),
    ]
    result = score_asset(values=_flat(40), hits=hits)
    assert result.direction == "conflicting"


# --- crypto_trend_score ----------------------------------------------


def test_crypto_long_is_up_and_short_is_down():
    from trading_list.scoring import crypto_trend_score

    assert crypto_trend_score("long", oi_change_pct=D("10"))[1] == "up"
    assert crypto_trend_score("short", pump_pct=D("50"))[1] == "down"
    assert crypto_trend_score("watch", pump_pct=D("50"))[1] == "down"


def test_crypto_base_score_given_for_passing_own_screen():
    """Крипта вже прошла власний скринінг — це і Є сигнал торгуємості,
    тож базовий внесок дається навіть без відомої магнітуди."""
    from trading_list.scoring import crypto_trend_score

    score, _, detail = crypto_trend_score("long", oi_change_pct=None)
    assert score == config.CRYPTO_SETUP_BASE_SCORE
    assert "невідома" in detail


def test_crypto_magnitude_saturates_at_one():
    from trading_list.scoring import crypto_trend_score

    score, _, _ = crypto_trend_score("short", pump_pct=D("9999"))
    assert score == Decimal("1")


def test_macro_damping_survives_hit_kind_rename():
    """Жива регресія 2026-10-04: типи хітів перейменували на
    release_upcoming/release_done, а перевірка приглушення шукала
    точний "release" — і тихо перестала працювати (каталізатор акцій
    0.18 → 0.71, 6 акцій заповнили список).

    Переписано 2026-10-13 під новий контракт: приглушення вже не
    залежить від `is_stock`, тому порівнюємо з НЕПРИГЛУШЕНОЮ вагою
    прицільної новини — КОЖЕН release-тип мусить важити менше за
    неї, інакше перейменування знову пройде непоміченим."""
    own_news, _ = catalyst_score([_news()])
    for kind in ("release_upcoming", "release_done", "forecast"):
        macro, _ = catalyst_score([CatalystHit(kind, config.CATALYST_WEIGHT_RELEASE_HIGH)])
        assert macro < own_news, f"{kind}: приглушення макро не застосувалось"
