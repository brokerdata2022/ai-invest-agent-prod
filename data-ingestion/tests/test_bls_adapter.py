"""
Тести адаптера BLS — на фрагменті реальної відповіді API (знято живим
запитом 2026-10-13), без мережі.
"""

from datetime import date
from decimal import Decimal

import pytest

from macro.bls_adapter import METRICS, BlsAdapter


def _response(series_id, points, status="REQUEST_SUCCEEDED"):
    return {
        "status": status,
        "Results": {"series": [{"seriesID": series_id, "data": points}]},
    }


def test_normalize_monthly_points():
    adapter = BlsAdapter("cpi")
    records = adapter.normalize(_response("CUSR0000SA0", [
        {"year": "2026", "period": "M08", "value": "334.131"},
        {"year": "2026", "period": "M07", "value": "333.500"},
    ]))

    assert [r.observed_at for r in records] == [date(2026, 8, 1), date(2026, 7, 1)]
    assert records[0].value == Decimal("334.131")
    assert all(r.source == "bls" for r in records)
    assert all(r.metric_id == "cpi" for r in records)


def test_normalize_strips_thousand_separators():
    """BLS віддає зайнятість як "159,044" — кома зламала б Decimal."""
    adapter = BlsAdapter("nonfarm_payrolls")
    records = adapter.normalize(_response("CES0000000001", [
        {"year": "2026", "period": "M09", "value": "159,044"},
    ]))
    assert records[0].value == Decimal("159044")


def test_annual_and_quarterly_periods_are_skipped():
    """M13 — річне середнє, Q01 — квартал. Для звірки з МІСЯЧНИМ рядом
    FRED вони не пара: порівняння річного середнього з місячним
    значенням дало б хибну розбіжність."""
    adapter = BlsAdapter("cpi")
    records = adapter.normalize(_response("CUSR0000SA0", [
        {"year": "2026", "period": "M08", "value": "334.131"},
        {"year": "2025", "period": "M13", "value": "330.000"},
        {"year": "2025", "period": "Q01", "value": "328.000"},
    ]))
    assert len(records) == 1
    assert records[0].observed_at == date(2026, 8, 1)


def test_failed_request_raises_despite_http_200():
    """BLS повертає HTTP 200 навіть на помилку, а причину кладе в
    `status`. Тихо віддати нуль записів тут означало б "даних немає" —
    рівно та пастка, через яку мʼякий троттл GDELT місяць ховав
    поломку потоку."""
    adapter = BlsAdapter("cpi")
    with pytest.raises(ValueError, match="запит не вдався"):
        adapter.normalize(_response("CUSR0000SA0", [], status="REQUEST_NOT_PROCESSED"))


def test_empty_series_raises():
    adapter = BlsAdapter("cpi")
    with pytest.raises(ValueError, match="жодної придатної"):
        adapter.normalize(_response("CUSR0000SA0", []))


def test_unknown_metric_rejected():
    with pytest.raises(ValueError, match="Немає серії BLS"):
        BlsAdapter("real_gdp")


def test_all_series_are_seasonally_adjusted():
    """КРИТИЧНО для звірки: FRED бере згладжені ряди (CPIAUCSL/
    CPILFESL/UNRATE/PAYEMS), тож пара мусить бути теж згладжена.
    Під час живої перевірки я спершу взяв незгладжений `CUUR0000SA0`
    і отримав 334.980 проти наших 334.131 — розбіжність 0.25%, яка
    виглядала як справжня знахідка, а була різновидом ряду. Якби це
    пішло в продакшен, шар звірки давав би хибну тривогу щодня й
    знецінився б."""
    for metric_id, series_id in METRICS.items():
        assert not series_id.startswith("CUUR"), (
            f"{metric_id}: {series_id} — НЕзгладжений ряд, не пара до FRED"
        )
