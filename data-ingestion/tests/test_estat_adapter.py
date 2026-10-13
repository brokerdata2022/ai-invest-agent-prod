"""
Тести EstatAdapter. Фікстури побудовані на підтвердженій документацією
структурі e-Stat API v3.0 (GET_STATS_DATA.STATISTICAL_DATA.CLASS_INF/
DATA_INF).

2026-09-13: перший живий прогін виявив, що ця таблиця CPI публікує
кілька рядків на кожен місяць під різними кодами виміру `tab` (сам
індекс окремо від %-змін м/м і р/р) — фікстури й тести нижче явно
покривають цей сценарій (resolve_dimensions резолвить tab, normalize
має захист від дублікатів на випадок недостатньої фільтрації).
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from macro.estat_adapter import EstatAdapter, METRICS, _find_class_code, _parse_estat_time

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _mock_get(response_payload: dict) -> MagicMock:
    mock_response = MagicMock()
    mock_response.json.return_value = response_payload
    mock_response.raise_for_status.return_value = None
    return MagicMock(return_value=mock_response)


@pytest.fixture
def meta_response():
    return _load_fixture("estat_meta_response.json")


@pytest.fixture
def cpi_response():
    return _load_fixture("estat_cpi_response.json")


@pytest.fixture
def cpi_response_mixed_tab():
    return _load_fixture("estat_cpi_response_mixed_tab.json")


@pytest.fixture
def adapter():
    return EstatAdapter(api_key="test-app-id", metric_id="japan_cpi")


def test_missing_app_id_rejected():
    with pytest.raises(ValueError):
        EstatAdapter(api_key="", metric_id="japan_cpi")


def test_unknown_metric_id_rejected():
    with pytest.raises(ValueError):
        EstatAdapter(api_key="test-app-id", metric_id="not_a_real_metric")


def test_stats_data_id_resolved_from_metric_id(adapter):
    assert adapter.stats_data_id == "0004052037"
    assert adapter.source == "estat"


def test_find_class_code_exact_match(meta_response):
    class_inf = meta_response["GET_STATS_DATA"]["STATISTICAL_DATA"]["CLASS_INF"]
    assert _find_class_code(class_inf, "area", "全国") == "00000"
    assert _find_class_code(class_inf, "cat01", "総合") == "0001"
    assert _find_class_code(class_inf, "tab", "指数") == "01"


def test_find_class_code_returns_none_if_absent(meta_response):
    class_inf = meta_response["GET_STATS_DATA"]["STATISTICAL_DATA"]["CLASS_INF"]
    assert _find_class_code(class_inf, "area", "存在しない地域") is None


def test_parse_estat_time_monthly():
    assert _parse_estat_time("2026年07月") == date(2026, 7, 1)


def test_parse_estat_time_unparsable_returns_none():
    assert _parse_estat_time("не дата") is None


def test_resolve_dimensions_picks_preferred_values(adapter, meta_response):
    adapter.session.get = _mock_get(meta_response)

    dims = adapter._resolve_dimensions()

    assert dims["area"] == "00000"       # 全国
    assert dims["cat01"] == "0001"       # 総合
    assert dims["tab"] == "01"           # 指数 — не 02/03 (%-зміни)
    assert "time" not in dims


def test_resolve_dimensions_single_value_dimension_needs_no_preference():
    # Таблиця, де cat01/area мають лише одне значення — резолвиться
    # без запису в _DIMENSION_PREFERENCES, бо неоднозначності нема.
    response = {
        "GET_STATS_DATA": {
            "STATISTICAL_DATA": {
                "CLASS_INF": {
                    "CLASS_OBJ": [
                        {"@id": "area", "@name": "地域", "CLASS": {"@code": "00000", "@name": "全国"}},
                        {"@id": "cat01", "@name": "類・品目", "CLASS": {"@code": "0001", "@name": "総合"}},
                    ]
                }
            }
        }
    }
    adapter = EstatAdapter(api_key="test-app-id", metric_id="japan_cpi")
    adapter.session.get = _mock_get(response)

    dims = adapter._resolve_dimensions()
    assert dims == {"area": "00000", "cat01": "0001"}


def test_resolve_dimensions_raises_for_unknown_multivalue_dimension():
    # Новий вимір із кількома значеннями, для якого немає запису в
    # _DIMENSION_PREFERENCES, має явно провалитись, а не мовчки
    # обрати перше-ліпше значення.
    response = {
        "GET_STATS_DATA": {
            "STATISTICAL_DATA": {
                "CLASS_INF": {
                    "CLASS_OBJ": [
                        {"@id": "area", "@name": "地域", "CLASS": {"@code": "00000", "@name": "全国"}},
                        {"@id": "cat01", "@name": "類・品目", "CLASS": {"@code": "0001", "@name": "総合"}},
                        {
                            "@id": "cat02",
                            "@name": "невідомий вимір",
                            "CLASS": [
                                {"@code": "A", "@name": "варіант A"},
                                {"@code": "B", "@name": "варіант B"},
                            ],
                        },
                    ]
                }
            }
        }
    }
    adapter = EstatAdapter(api_key="test-app-id", metric_id="japan_cpi")
    adapter.session.get = _mock_get(response)

    with pytest.raises(ValueError, match="cat02"):
        adapter._resolve_dimensions()


def test_resolve_dimensions_raises_if_required_dims_missing(adapter):
    broken_response = {"GET_STATS_DATA": {"STATISTICAL_DATA": {"CLASS_INF": {"CLASS_OBJ": []}}}}
    adapter.session.get = _mock_get(broken_response)

    with pytest.raises(ValueError):
        adapter._resolve_dimensions()


def test_fetch_passes_resolved_dimensions_as_cd_params(adapter, meta_response, cpi_response):
    meta_mock = MagicMock()
    meta_mock.json.return_value = meta_response
    meta_mock.raise_for_status.return_value = None

    data_mock = MagicMock()
    data_mock.json.return_value = cpi_response
    data_mock.raise_for_status.return_value = None

    adapter.session.get = MagicMock(side_effect=[meta_mock, data_mock])

    adapter.fetch()

    # Другий виклик — основний запит даних; перевіряємо, що коди
    # виміру tab теж передані (не тільки area/cat01, як у старій версії).
    _, kwargs = adapter.session.get.call_args_list[1]
    params = kwargs["params"]
    assert params["cdArea"] == "00000"
    assert params["cdCat01"] == "0001"
    assert params["cdTab"] == "01"


def test_normalize_produces_expected_records(adapter, cpi_response):
    records = adapter.normalize(cpi_response)

    # Третє значення фікстури — "-" (немає даних), має бути пропущене.
    assert len(records) == 2

    first, second = records
    assert first.observed_at == date(2026, 6, 1)
    assert first.value == Decimal("101.5")
    assert second.observed_at == date(2026, 7, 1)
    assert second.value == Decimal("101.9")
    for r in records:
        assert r.source == "estat"
        assert r.metric_id == "japan_cpi"
        assert r.revision is None


def test_normalize_deduplicates_conflicting_values_for_same_date(adapter, cpi_response_mixed_tab):
    # Захисний сценарій: якщо фільтрація за tab чомусь не спрацювала
    # (напр. новий tab-код без запису в _DIMENSION_PREFERENCES), у
    # відповіді все одно можуть опинитись і індекс (101.5), і %-зміна
    # (1.9) за ту саму дату. normalize() має залишити ОДНЕ значення
    # (перше в списку), а не створювати два записи, які потім
    # запишуться в БД як фальшиві "ревізії" одне одного.
    records = adapter.normalize(cpi_response_mixed_tab)

    assert len(records) == 1
    assert records[0].observed_at == date(2026, 6, 1)
    assert records[0].value == Decimal("101.5")  # перше значення (індекс), не 1.9 (%)


def test_normalize_applies_limit(adapter, cpi_response):
    records = adapter.normalize(cpi_response, limit=1)
    assert len(records) == 1
    assert records[0].observed_at == date(2026, 7, 1)


def test_normalize_returns_empty_on_empty_value_list(adapter):
    empty_response = {
        "GET_STATS_DATA": {
            "STATISTICAL_DATA": {
                "CLASS_INF": {"CLASS_OBJ": []},
                "DATA_INF": {"VALUE": []},
            }
        }
    }
    assert adapter.normalize(empty_response) == []


def test_all_declared_metrics_have_stats_data_id():
    for metric_id, stats_data_id in METRICS.items():
        assert isinstance(metric_id, str) and metric_id
        assert isinstance(stats_data_id, str) and stats_data_id.isdigit()
