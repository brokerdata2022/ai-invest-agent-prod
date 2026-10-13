"""
Тести адаптера Yahoo chart — на фікстурі відповіді, без мережі
(data-ingestion/CLAUDE.md: "адаптер можна протестувати окремо, подавши
йому фейкову відповідь API").
"""

from datetime import date
from decimal import Decimal

import pytest

from quotes.yahoo_chart_adapter import (
    DEFAULT_RANGE,
    SYMBOLS,
    YahooChartAdapter,
    _range_for,
)


def _response(timestamps, closes):
    return {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {"quote": [{"close": closes}]},
                }
            ]
        }
    }


def test_normalize_maps_timestamps_to_dates():
    # 2026-10-09 / 2026-10-12 UTC
    adapter = YahooChartAdapter("brent_crude")
    records = adapter.normalize(_response(
        [1791504000, 1791763200], [103.23, 106.65],
    ))

    assert [r.observed_at for r in records] == [date(2026, 10, 9), date(2026, 10, 12)]
    assert [r.value for r in records] == [Decimal("103.23"), Decimal("106.65")]
    assert all(r.source == "yahoo_chart" for r in records)
    assert all(r.metric_id == "brent_crude" for r in records)
    # revision проставляє шар збереження, не адаптер (common/adapter.py)
    assert all(r.revision is None for r in records)


def test_normalize_skips_null_closes():
    """Yahoo віддає свічку й для днів, коли інструмент не торгувався
    (свята різних бірж) — там close=null. Пропускаємо, а не пишемо
    нуль: нуль у ціновому ряді зламав би будь-який розрахунок тренду."""
    adapter = YahooChartAdapter("xauusd")
    records = adapter.normalize(_response(
        [1791504000, 1791590400, 1791763200], [4172.68, None, 4194.50],
    ))

    assert len(records) == 2
    assert [r.value for r in records] == [Decimal("4172.68"), Decimal("4194.50")]


def test_unexpected_structure_raises_loudly():
    """Неофіційне джерело: формат може змінитись без попередження.
    Тихий нуль записів тут гірший за виняток — рівно той урок, що
    мʼякий троттл GDELT, який до 2026-10-05 читався як "немає
    новин" і місяць убивав потік непомітно."""
    adapter = YahooChartAdapter("wti_crude")
    with pytest.raises(ValueError, match="несподівана структура"):
        adapter.normalize({"chart": {"result": []}})


def test_mismatched_lengths_raise():
    adapter = YahooChartAdapter("wti_crude")
    with pytest.raises(ValueError, match="різної"):
        adapter.normalize(_response([1791504000, 1791590400], [90.0]))


def test_all_null_closes_raise_instead_of_empty_list():
    adapter = YahooChartAdapter("natgas")
    with pytest.raises(ValueError, match="жодної"):
        adapter.normalize(_response([1791504000], [None]))


def test_unknown_metric_id_rejected_at_construction():
    with pytest.raises(ValueError, match="Немає символу Yahoo"):
        YahooChartAdapter("definitely_not_an_asset")


def test_range_for_picks_smallest_covering_period():
    """`limit` — єдиний інтерфейс збору в проєкті (run_collect.py
    викликає ним ВСІ адаптери), а Yahoo приймає період — звідси
    переклад."""
    assert _range_for(5) == "5d"
    assert _range_for(20) == "1mo"
    assert _range_for(65) == "6mo"
    assert _range_for(130) == "1y"
    assert _range_for(None) == DEFAULT_RANGE
    assert _range_for(10**9 + 1) == "max"


def test_covers_every_watchlist_asset_class():
    """Адаптер мусить покривати всі три класи watchlist-активів:
    товари, форекс і крипту — саме через те, що для кожного з них
    попереднє джерело підводило (FRED-викид, відсутність історії)."""
    assert {"brent_crude", "wti_crude", "xauusd", "xagusd", "natgas", "coffee"} <= set(SYMBOLS)
    assert {"eurusd", "usdjpy"} <= set(SYMBOLS)
    assert {"btc", "eth", "sol"} <= set(SYMBOLS)
