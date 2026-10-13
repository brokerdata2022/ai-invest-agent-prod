"""
Тести backtest_llm_metric()/format_verdict() — forecast_fn
інжектується (фейкова функція замість LLM), тож мережі/БД не потрібно.
Той самий підхід, що test_backtest.py для лінійної моделі.
"""

from types import SimpleNamespace

from forecasting.backtest_llm import (
    backtest_llm_metric,
    format_verdict,
    resolve_backtest_route,
)
from llm_common import resolve_route

# fetch_recent() віддає НАЙНОВІШЕ ПЕРШЕ — саме в такому порядку
# backtest_llm_metric() очікує вхід.
OBSERVATIONS_DESC = [
    {"observed_at": "2026-08-31", "value": 110.0},
    {"observed_at": "2026-07-31", "value": 108.0},
    {"observed_at": "2026-06-30", "value": 106.0},
    {"observed_at": "2026-05-31", "value": 104.0},
    {"observed_at": "2026-04-30", "value": 102.0},
    {"observed_at": "2026-03-31", "value": 100.0},
]


def _perfect_forecast(history_rows):
    """Знає справжній крок серії (+2) — ідеальний прогноз."""
    return float(history_rows[-1]["value"]) + 2.0


def test_backtest_checks_only_requested_number_of_points():
    result = backtest_llm_metric(
        "cpi", OBSERVATIONS_DESC, _perfect_forecast, points=2, min_history=3
    )
    assert result["n_points"] == 2
    assert result["n_skipped"] == 0


def test_backtest_respects_min_history_floor():
    """points=99 не може опуститись нижче min_history — інакше перші
    точки прогнозувались би з порожньої історії."""
    result = backtest_llm_metric(
        "cpi", OBSERVATIONS_DESC, _perfect_forecast, points=99, min_history=4
    )
    assert result["n_points"] == len(OBSERVATIONS_DESC) - 4


def test_backtest_perfect_forecast_beats_naive():
    result = backtest_llm_metric(
        "cpi", OBSERVATIONS_DESC, _perfect_forecast, points=3, min_history=3
    )
    assert result["llm_mae"] == 0.0
    assert result["naive_mae"] > 0
    assert "LLM точніший" in format_verdict(result)


def test_backtest_forecast_fn_never_sees_the_future():
    """Честний out-of-sample: у кожному виклику історія обривається
    ДО точки, що перевіряється."""
    seen = []

    def spy(history_rows):
        seen.append([r["observed_at"] for r in history_rows])
        return float(history_rows[-1]["value"])

    backtest_llm_metric("cpi", OBSERVATIONS_DESC, spy, points=2, min_history=4)

    chronological_dates = [o["observed_at"] for o in OBSERVATIONS_DESC[::-1]]
    assert seen == [chronological_dates[:4], chronological_dates[:5]]


def test_backtest_skipped_points_are_counted_not_hidden():
    """None-прогноз (збійна відповідь LLM) не рахується як влучення —
    точка пропускається й це видно в n_skipped."""
    calls = {"n": 0}

    def flaky(history_rows):
        calls["n"] += 1
        return None if calls["n"] == 1 else float(history_rows[-1]["value"]) + 2.0

    result = backtest_llm_metric(
        "cpi", OBSERVATIONS_DESC, flaky, points=2, min_history=4
    )
    assert result["n_points"] == 1
    assert result["n_skipped"] == 1
    assert result["llm_mae"] == 0.0


def test_backtest_baselines_exclude_points_llm_skipped():
    """Пропущена точка виходить і з базових ліній — інакше MAE
    порівнювались би на РІЗНИХ наборах точок (живий кейс
    `unemployment_rate` 2026-10-04: naive отримував безкоштовну точку з
    похибкою 0, і вердикт перевертався)."""
    result = backtest_llm_metric(
        "cpi", OBSERVATIONS_DESC, lambda rows: None, points=2, min_history=4
    )
    assert result["n_points"] == 0
    assert result["n_skipped"] == 2
    assert result["llm_mae"] is None
    assert result["naive_mae"] is None
    assert result["trend_mae"] is None


def test_backtest_all_three_maes_measured_on_equal_point_counts():
    """Головна гарантія честності порівняння: скільки точок у LLM —
    стільки ж у кожної базової лінії."""
    calls = {"n": 0}

    def flaky(history_rows):
        calls["n"] += 1
        return None if calls["n"] == 1 else float(history_rows[-1]["value"]) + 2.0

    result = backtest_llm_metric(
        "cpi", OBSERVATIONS_DESC, flaky, points=3, min_history=3
    )
    # 3 точки запрошено, 1 пропущено → 2 в кожній з трьох моделей.
    assert result["n_points"] == 2
    assert result["n_skipped"] == 1
    assert result["llm_mae"] is not None
    assert result["naive_mae"] == 2.0  # рівний крок +2 на обох точках
    assert result["trend_mae"] is not None


def test_format_verdict_without_points():
    verdict = format_verdict(
        {"llm_mae": None, "naive_mae": 1.0, "trend_mae": None, "n_points": 0}
    )
    assert "вердикту немає" in verdict


def test_format_verdict_naive_wins():
    verdict = format_verdict(
        {"llm_mae": 2.0, "naive_mae": 1.0, "trend_mae": None, "n_points": 3}
    )
    assert "naive кращий" in verdict
    assert "100.0%" in verdict


def test_format_verdict_exact_tie_reads_as_tie():
    """Живий кейс `unemployment_rate` (2026-10-04): MAE рівно однакові,
    а вивід казав "naive кращий — LLM гірший на 0.0%"."""
    verdict = format_verdict(
        {"llm_mae": 0.0625, "naive_mae": 0.0625, "trend_mae": 0.1263, "n_points": 8}
    )
    assert "нічия" in verdict
    assert "гірший" not in verdict


def test_format_verdict_handles_flawless_naive():
    """Плоска серія: naive MAE = 0, відносна різниця не визначена —
    ділення на нуль замість вердикту було б падінням."""
    verdict = format_verdict(
        {"llm_mae": 0.5, "naive_mae": 0.0, "trend_mae": None, "n_points": 4}
    )
    assert "безпомилковий" in verdict


# ============================================================
# Перекриття маршруту з CLI
# ============================================================
# Додано 2026-10-05: бектест за визначенням міряє РІЗНІ моделі на тих
# самих точках (живий кейс — порівняти Opus 5 і Sonnet 5), а правити
# для цього config.py означало б заодно перемкнути бойовий прогноз.


class _Args:
    def __init__(self, provider=None, model=None, effort=None):
        self.provider = provider
        self.model = model
        self.effort = effort


def test_route_comes_from_config_without_flags(monkeypatch):
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    route = resolve_backtest_route(_Args())
    assert route == resolve_route("metric_forecast_backtest")


def test_model_flag_overrides_only_the_model(monkeypatch):
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    configured = resolve_route("metric_forecast_backtest")

    route = resolve_backtest_route(_Args(model="claude-sonnet-5"))

    assert route.model == "claude-sonnet-5"
    assert route.provider == configured.provider
    # effort не чіпається — інакше порівняння моделей змінювало б
    # ДВІ змінні одночасно й нічого не доводило.
    assert route.effort == configured.effort


def test_provider_and_effort_flags_apply(monkeypatch):
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    route = resolve_backtest_route(_Args(provider="deepseek", model="deepseek-chat", effort=None))
    assert (route.provider, route.model) == ("deepseek", "deepseek-chat")

    route = resolve_backtest_route(_Args(effort="low"))
    assert route.effort == "low"


# ============================================================
# Бектест мусить міряти ТЕ САМЕ, що працює в продакшені
# ============================================================
# Живий збій 2026-10-06: історію добрали з 14-30 точок до ~285, і
# бектест почав віддавати моделі УСЮ історію (190 рядків, промпт виріс
# у 5-6 разів), тоді як `forecast_metric.run_forecast()` і далі давав
# 24 точки. Виміряна точність перестала стосуватись бойового
# прогнозу — і це було невидимо, доки історія була коротка.


def test_backtest_uses_production_history_window(monkeypatch):
    """Промпт бектесту будується на тому самому вікні, що бойовий
    прогноз (`config.FORECAST_HISTORY_POINTS`), а не на всій історії."""
    import config
    from forecasting import backtest_llm

    captured = {}

    def fake_build_prompt(metric_id, label, rows, include_deltas=False, market_context=None):
        captured["rows"] = len(rows)
        captured["include_deltas"] = include_deltas
        captured["market_context"] = market_context
        return "PROMPT"

    monkeypatch.setattr(backtest_llm, "build_prompt", fake_build_prompt)
    monkeypatch.setattr(backtest_llm, "describe_metric", lambda m: m)
    monkeypatch.setattr(
        backtest_llm, "call_llm",
        lambda prompt, system, purpose=None, route=None: SimpleNamespace(
            text='{"forecast_value": 1}', provider="deepseek", model="deepseek-chat",
            fallback_from=None,
        ),
    )
    monkeypatch.setattr(backtest_llm, "log_llm_call", lambda *a, **k: 1)
    monkeypatch.setattr(backtest_llm, "parse_forecast_response", lambda raw: SimpleNamespace(forecast_value=1.0))
    monkeypatch.setattr(backtest_llm, "is_plausible", lambda values, v: (True, ""))

    # Історія ВТРИЧІ довша за продакшен-вікно.
    long_history = [
        {"observed_at": f"2026-01-{i:02d}", "value": float(i)}
        for i in range(1, config.FORECAST_HISTORY_POINTS * 3)
    ]
    backtest_llm.make_llm_forecast_fn(conn=None, metric_id="cpi")(long_history)

    assert captured["rows"] == config.FORECAST_HISTORY_POINTS, (
        "бектест дав моделі інше вікно історії, ніж бойовий прогноз — "
        "виміряна точність не стосуватиметься продакшену"
    )


def test_production_default_limit_equals_the_config_window():
    """Друга половина тієї самої гарантії: бойовий прогноз не має
    власного літерала вікна."""
    import inspect

    import config
    from forecasting.forecast_metric import run_forecast

    assert inspect.signature(run_forecast).parameters["limit"].default == config.FORECAST_HISTORY_POINTS


def test_delta_flag_switches_the_prompt_variant(monkeypatch):
    """Варіант промпта мусить бути ПАРАМЕТРОМ виміру, як `--model`:
    інакше порівняти "з Δ" і "без Δ" можна лише правкою коду посеред
    серії прогонів — а саме це двічі за 2026-10-06 зробило таблицю
    результатів несумісною самою з собою.

    Дефолт — БЕЗ Δ: A/B на 8 показниках показав, що Δ зашкодив 6 із
    них (докладно — `llm_forecast.PROMPT_VARIANT`)."""
    from forecasting import backtest_llm

    captured = {}

    def fake_build_prompt(metric_id, label, rows, include_deltas=False, market_context=None):
        captured["include_deltas"] = include_deltas
        return "PROMPT"

    monkeypatch.setattr(backtest_llm, "build_prompt", fake_build_prompt)
    monkeypatch.setattr(backtest_llm, "describe_metric", lambda m: m)
    monkeypatch.setattr(
        backtest_llm, "call_llm",
        lambda prompt, system, purpose=None, route=None: SimpleNamespace(
            text='{"forecast_value": 1}', provider="deepseek", model="deepseek-chat",
            fallback_from=None,
        ),
    )
    monkeypatch.setattr(backtest_llm, "log_llm_call", lambda *a, **k: 1)
    monkeypatch.setattr(backtest_llm, "parse_forecast_response", lambda raw: SimpleNamespace(forecast_value=1.0))
    monkeypatch.setattr(backtest_llm, "is_plausible", lambda values, v: (True, ""))

    history = [{"observed_at": "2026-01-01", "value": 1.0}, {"observed_at": "2026-02-01", "value": 2.0}]

    backtest_llm.make_llm_forecast_fn(None, "cpi", include_deltas=True)(history)
    assert captured["include_deltas"] is True

    # Дефолт продакшену — рівні без Δ (зміряно A/B 2026-10-06).
    backtest_llm.make_llm_forecast_fn(None, "cpi")(history)
    assert captured["include_deltas"] is False
