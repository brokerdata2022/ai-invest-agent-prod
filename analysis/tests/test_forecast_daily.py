"""
Тести forecast_daily_cycle() і resolve_source() для ДЕННИХ серій без
календаря релізів (облігації/ставка/USDJPY, 2026-10-04 — "облігації це
обовязково"). Підмінені run_forecast/has_forecast_for_latest, без БД і
без LLM — той самий підхід, що test_update_forecasts.py.
"""

import pytest

from forecasting._metric_source import resolve_source
from forecasting.forecast_metric import FORECASTABLE_METRICS
from forecasting import forecast_daily
from forecasting.forecast_daily import forecast_daily_cycle

API_KEY = "fake-key"

# Денні серії, які прогнозуються ЩОДНЯ. `fed_funds_rate` свідомо НЕ
# тут із 2026-10-11: ставка прогнозується на ЗАСІДАННЯ, не щодня
# (config.FORECAST_POLICY_RATE_METRICS) — між засіданнями щоденна
# ефективна ставка стоїть на місці, і денний прогноз для неї це
# витрата виклику на "як вчора".
DAILY_METRICS = {"treasury_10y", "treasury_2y", "usdjpy_fx_rate"}


# --- реєстрація денних серій ----------------------------------------


@pytest.mark.parametrize("metric_id", sorted(DAILY_METRICS))
def test_daily_metrics_resolve_to_fred(metric_id):
    """До 2026-10-04 resolve_source() падав на цих показниках
    ("невідомий metric_id"), тож прогноз для облігацій був технічно
    неможливий."""
    assert resolve_source(metric_id) == "fred"


@pytest.mark.parametrize("metric_id", sorted(DAILY_METRICS))
def test_daily_metrics_are_forecastable(metric_id):
    assert metric_id in FORECASTABLE_METRICS


def test_calendar_metrics_still_forecastable():
    """Денні серії ДОДАНІ, а не замінили календарні."""
    assert {"cpi", "nonfarm_payrolls", "japan_cpi"} <= FORECASTABLE_METRICS


def test_resolve_source_still_rejects_unknown_metric():
    with pytest.raises(ValueError, match="Невідомий metric_id"):
        resolve_source("made_up_metric")


# --- forecast_daily_cycle -------------------------------------------


def _patch(monkeypatch, *, has_existing, run_result):
    monkeypatch.setattr(
        forecast_daily, "has_forecast_for_latest",
        lambda conn, source, metric_id: has_existing,
    )
    calls = []

    def fake_run_forecast(conn, metric_id):
        calls.append(metric_id)
        if isinstance(run_result, Exception):
            raise run_result
        return run_result

    monkeypatch.setattr(forecast_daily, "run_forecast", fake_run_forecast)
    return calls


def test_cycle_forecasts_every_daily_metric(monkeypatch):
    calls = _patch(monkeypatch, has_existing=False, run_result=7)

    result = forecast_daily_cycle(conn=None)

    assert set(result["forecasted"]) == DAILY_METRICS
    # Ставка центробанку пропускається завжди — вона прогнозується на
    # засідання (2026-10-11), не денним циклом.
    assert result["skipped"] == ["fed_funds_rate"]
    assert result["failed"] == []
    assert set(calls) == DAILY_METRICS


def test_cycle_skips_without_calling_llm_when_forecast_already_exists(monkeypatch):
    """Головна причина існування --skip-existing: FRED публікує денні
    серії з лагом день-два, тож кілька прогонів поспіль бачать ТЕ САМЕ
    останнє значення — без перевірки платили б за однаковий прогноз
    щодня."""
    calls = _patch(monkeypatch, has_existing=True, run_result=7)

    result = forecast_daily_cycle(conn=None)

    # `fed_funds_rate` теж у skipped, але з ІНШОЇ причини — ставка
    # прогнозується на засідання (2026-10-11), не через --skip-existing.
    assert set(result["skipped"]) == DAILY_METRICS | {"fed_funds_rate"}
    assert result["forecasted"] == []
    assert calls == [], "run_forecast (а з ним LLM-виклик) не має викликатись"


def test_cycle_honours_no_skip_existing(monkeypatch):
    calls = _patch(monkeypatch, has_existing=True, run_result=7)

    result = forecast_daily_cycle(conn=None, skip_existing=False)

    assert set(result["forecasted"]) == DAILY_METRICS
    assert set(calls) == DAILY_METRICS


def test_cycle_counts_none_result_as_failed(monkeypatch):
    """None з run_forecast — прогноз не збережено (замало історії, гейт
    правдоподібності, збійна відповідь); це провал, не успіх."""
    _patch(monkeypatch, has_existing=False, run_result=None)

    result = forecast_daily_cycle(conn=None)

    assert result["forecasted"] == []
    assert set(result["failed"]) == DAILY_METRICS


def test_cycle_continues_after_one_metric_raises(monkeypatch):
    rollbacks = []

    class FakeConn:
        def rollback(self):
            rollbacks.append(1)

    monkeypatch.setattr(
        forecast_daily, "has_forecast_for_latest",
        lambda conn, source, metric_id: False,
    )
    seen = []

    def fake_run_forecast(conn, metric_id):
        seen.append(metric_id)
        # Падаємо на одному з ДЕННИХ показників (fed_funds_rate більше
        # не проходить через денний цикл — ставка йде на засідання).
        if metric_id == "treasury_2y":
            raise RuntimeError("boom")
        return 1

    monkeypatch.setattr(forecast_daily, "run_forecast", fake_run_forecast)

    result = forecast_daily_cycle(conn=FakeConn())

    # Решта показників усе одно опрацьована, попри виняток на одному.
    assert set(seen) == DAILY_METRICS
    assert result["failed"] == ["treasury_2y"]
    assert set(result["forecasted"]) == DAILY_METRICS - {"treasury_2y"}
    assert len(rollbacks) == 1


def test_policy_rate_is_skipped_by_the_daily_cycle(monkeypatch):
    """Прямий фідбек користувача 2026-10-11: "ставка прогнозується
    після виходу нових макроекономічних даних + новини + виступи
    банків та голів комісій" — тобто на ЗАСІДАННЯ. Денний цикл її не
    чіпає, інакше кожен день витрачався б виклик на незмінне
    значення."""
    import config
    from forecasting import forecast_daily as module

    calls = []
    monkeypatch.setattr(module, "run_forecast", lambda conn, metric_id: calls.append(metric_id) or 1)
    monkeypatch.setattr(module, "has_forecast_for_latest", lambda conn, source, metric_id: False)
    monkeypatch.setattr(module, "resolve_source", lambda metric_id: "fred")

    result = module.forecast_daily_cycle(conn=None)

    for metric_id in config.FORECAST_POLICY_RATE_METRICS:
        assert metric_id not in calls
    assert "fed_funds_rate" in result["skipped"]
