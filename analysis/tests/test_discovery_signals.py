"""
Арифметика пошуку новачків (`discovery/signals.py`) + самі детектори
руху — чисті функції, без БД і мережі.

Головне, що тут стережеться, — причина, через яку стара реалізація
давала **0 рядків за весь час**: кандидат зникав беззвучно. Тому
окремі тести на те, що (1) падіння — такий самий кандидат, як
зростання, (2) поріг входу це РУХ, а не скор, (3) квота детектора не
дає крипті витіснити акції.
"""

from decimal import Decimal

import pytest

import config
from discovery import crypto_movers, stock_movers
from discovery.signals import (
    Candidate,
    direction_from_move,
    max_daily_move_pct,
    pct_change,
    saturating_ratio,
    score_candidate,
    select_top,
    volume_spike_pct,
)


# ============================================================
# Базова арифметика
# ============================================================


def test_pct_change_basic():
    assert pct_change(Decimal("100"), Decimal("112")) == Decimal("12")
    assert pct_change(Decimal("100"), Decimal("88")) == Decimal("-12")


@pytest.mark.parametrize("old,new", [(None, Decimal("1")), (Decimal("1"), None), (Decimal("0"), Decimal("1"))])
def test_pct_change_unknown_is_none_not_zero(old, new):
    """"Невідомо" і "не змінилось" — різні речі, і скор мусить їх
    розрізняти (інакше актив без історії отримав би такий самий
    нульовий рух, як справді нерухомий)."""
    assert pct_change(old, new) is None


def test_volume_spike_against_window_mean():
    volumes = [Decimal("1000"), Decimal("1000"), Decimal("1000"), Decimal("2000")]
    assert volume_spike_pct(volumes, window=3) == Decimal("100")


def test_volume_spike_needs_history():
    """База з одного дня дала б випадковий сигнал на першому ж тихому
    дні — краще "невідомо"."""
    assert volume_spike_pct([Decimal("1000"), Decimal("5000")]) is None


def test_direction_covers_both_ways():
    assert direction_from_move(Decimal("5")) == "up"
    assert direction_from_move(Decimal("-5")) == "down"
    assert direction_from_move(Decimal("0")) == "neutral"
    assert direction_from_move(None) == "unclear"


def test_saturating_ratio_caps_at_one():
    assert saturating_ratio(Decimal("20"), Decimal("40")) == Decimal("0.5")
    assert saturating_ratio(Decimal("400"), Decimal("40")) == Decimal("1")
    # За модулем: обвал насичує сигнал так само, як памп.
    assert saturating_ratio(Decimal("-400"), Decimal("40")) == Decimal("1")
    assert saturating_ratio(None, Decimal("40")) == Decimal("0")


def test_score_symmetric_for_up_and_down():
    kwargs = dict(
        move_saturation_pct=Decimal("40"),
        w_move=config.DISCOVERY_W_MOVE,
        w_volume=config.DISCOVERY_W_VOLUME,
        w_news=config.DISCOVERY_W_NEWS,
    )
    assert score_candidate(move_pct=Decimal("20"), **kwargs) == score_candidate(
        move_pct=Decimal("-20"), **kwargs
    )


def test_score_weights_sum_to_one():
    """Інакше "сила сигналу 0..1" перестала б означати те, що
    написано, і `DISCOVERY_MIN_SCORE` калібрувався б у нікуди."""
    assert (
        config.DISCOVERY_W_MOVE + config.DISCOVERY_W_VOLUME + config.DISCOVERY_W_NEWS
        == Decimal("1")
    )


def test_min_score_reachable_by_move_alone():
    """Регресія на головну причину "0 рядків": кандидат лише з рухом
    ціни (найчастіший випадок — обсягу/новин по ньому немає) мусить
    проходити санітарний поріг, інакше список знову буде порожнім."""
    at_threshold = score_candidate(
        move_pct=config.DISCOVERY_CRYPTO_MIN_MOVE_PCT,
        move_saturation_pct=config.DISCOVERY_CRYPTO_SATURATION_PCT,
        w_move=config.DISCOVERY_W_MOVE,
        w_volume=config.DISCOVERY_W_VOLUME,
        w_news=config.DISCOVERY_W_NEWS,
    )
    assert at_threshold >= config.DISCOVERY_MIN_SCORE


# ============================================================
# Відбір фіналістів
# ============================================================


def _candidate(asset_id, detector, score):
    return Candidate(
        asset_id=asset_id,
        label=asset_id,
        asset_class="crypto" if detector == "crypto_movers" else "stock",
        detector=detector,
        direction="up",
        reason="r",
        score=Decimal(score),
    )


def test_select_top_respects_detector_quota():
    """Крипта завжди дає найбільше й найсильніше рухів — без квоти
    список новачків став би другим крипто-скринінгом."""
    crypto = [_candidate(f"C{i}USDT", "crypto_movers", "0.9") for i in range(10)]
    stocks = [_candidate(f"S{i}", "stock_movers", "0.3") for i in range(3)]

    selected = select_top(crypto + stocks, min_score=Decimal("0.1"), max_items=12, max_per_detector=6)

    assert sum(1 for c in selected if c.detector == "crypto_movers") == 6
    assert sum(1 for c in selected if c.detector == "stock_movers") == 3


def test_select_top_filters_and_sorts():
    rows = [_candidate("A", "stock_movers", "0.5"), _candidate("B", "stock_movers", "0.05"),
            _candidate("C", "stock_movers", "0.8")]
    selected = select_top(rows, min_score=Decimal("0.1"), max_items=10, max_per_detector=10)
    assert [c.asset_id for c in selected] == ["C", "A"]


# ============================================================
# Детектор крипти
# ============================================================


def _aggregated(symbol, changes, volume="10000000"):
    return {
        symbol: {
            "quote_volume_24h": Decimal(volume),
            "exchange_count": len(changes),
            "exchanges": [f"ex{i}" for i in range(len(changes))],
            "per_exchange": {
                f"ex{i}": {"price_change_percent": Decimal(c)} for i, c in enumerate(changes)
            },
        }
    }


def test_extreme_move_takes_biggest_by_abs_not_max():
    """`pump_pct_from_aggregated()` крипто-скринінгу бере max() — для
    обвалу це вернуло б найменш негативне значення й применшило б
    подію. Тут потрібен максимум ЗА МОДУЛЕМ."""
    entry = _aggregated("XUSDT", ["-25", "-5", "-18"])["XUSDT"]
    assert crypto_movers.extreme_move_pct(entry) == Decimal("-25")


def test_crypto_detects_crash_not_only_pump():
    crash = crypto_movers.detect(_aggregated("XUSDT", ["-19"]), {}, tracked=set())
    assert len(crash) == 1
    assert crash[0].direction == "down"
    assert crash[0].asset_class == "crypto"


def test_crypto_skips_thin_market():
    """20% руху на $100 тис обсягу — це дві угоди, не подія."""
    assert crypto_movers.detect(_aggregated("XUSDT", ["20"], volume="100000"), {}, set()) == []


def test_crypto_skips_tracked_by_base_coin():
    """У watchlist asset_id записаний як 'btc', а символ ф'ючерса —
    'BTCUSDT': без зведення до базової монети BTC приходив би як
    "новачок" щодня."""
    assert crypto_movers.detect(_aggregated("BTCUSDT", ["20"]), {}, {"BTC"}) == []


def test_crypto_skips_non_usdt_duplicate_listing():
    """BTCUSDC — той самий BTC під іншою назвою; bulk-знімок Binance
    віддає й такі, і без фільтра watchlist-актив приходив би
    "новачком"."""
    assert crypto_movers.detect(_aggregated("BTCUSDC", ["20"]), {}, {"BTC"}) == []


def test_crypto_volume_spike_raises_score():
    quiet = crypto_movers.detect(_aggregated("XUSDT", ["20"]), {}, set())[0]
    spiking = crypto_movers.detect(
        _aggregated("XUSDT", ["20"]),
        {"XUSDT": [Decimal("1000000"), Decimal("1000000"), Decimal("3000000")]},
        set(),
    )[0]
    assert spiking.score > quiet.score


# ============================================================
# Детектор акцій
# ============================================================


def _series(closes, volumes=None):
    return {"ACME": {"closes": [Decimal(c) for c in closes], "volumes": [Decimal(v) for v in (volumes or [])]}}


def test_stock_move_measured_over_trading_points():
    closes = ["100"] * 20 + ["90"]  # -10% за останні 5 точок
    found = stock_movers.detect(_series(closes), tracked=set())
    assert len(found) == 1
    assert found[0].direction == "down"
    assert found[0].signals["move_pct_short"] == Decimal("-10")


def test_stock_skips_quiet_series():
    closes = ["100", "100.5", "99.8", "100.2", "100", "100.1", "100"]
    assert stock_movers.detect(_series(closes), tracked=set()) == []


def test_stock_skips_too_short_history():
    assert stock_movers.detect(_series(["100", "150"]), tracked=set()) == []


# ============================================================
# Гейт правдоподібності — корпоративні дії
# ============================================================


def test_max_daily_move_finds_the_jump():
    closes = [Decimal(x) for x in ["77.74", "77.87", "77.65", "12.57", "11.92"]]
    jump = max_daily_move_pct(closes)
    assert jump is not None and jump < Decimal("-80")


def test_corporate_action_is_rejected_with_reason(caplog):
    """Живий кейс CTVA (2026-10-05): спін-оф дав "рух" -84.8% за 5 днів
    і ТОП-1 кандидата зі score 0.85. Ряд Twelve Data не скоригований на
    корпоративну дію, тож такий стрибок мусить відкидатись — і саме з
    назвою причини в лозі, а не тихо."""
    closes = ["77.0"] * 20 + ["77.74", "77.87", "77.65", "12.57", "11.92"]
    series = {"CTVA": {"closes": [Decimal(c) for c in closes], "volumes": []}}

    with caplog.at_level("INFO"):
        found = stock_movers.detect(series, tracked=set())

    assert found == []
    assert "корпоративну дію" in caplog.text


def test_real_earnings_crash_still_passes():
    """Гейт не має права викидати справжній обвал на звітності —
    -20% за день це в межах правдоподібного для акції."""
    closes = ["100"] * 20 + ["100", "100", "100", "100", "80"]
    found = stock_movers.detect({"ACME": {"closes": [Decimal(c) for c in closes], "volumes": []}}, set())
    assert len(found) == 1
    assert found[0].direction == "down"


def test_stock_uses_company_name_when_known():
    closes = ["100"] * 20 + ["130"]
    found = stock_movers.detect(_series(closes), set(), {"ACME": "Acme Robotics"})
    assert found[0].label == "Acme Robotics"
    assert found[0].direction == "up"
