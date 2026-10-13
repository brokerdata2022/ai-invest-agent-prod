"""
Вибір методу прогнозу за показником (`forecasting/method_selector.py`)
— чисті функції, без БД і мережі.

Кожен тест прив'язаний до ЗМІРЯНОГО випадку з трьох бектестів
2026-10-05 (таблиці — docs/archive/plan-journal-2026-10-11.md, Фаза 2), а не до вигаданих чисел: саме
ці 19 показників і показали, що метод мусить обиратись за показником.
"""

import config
from forecasting.method_selector import (
    METHOD_LLM,
    METHOD_LLM_UNVALIDATED,
    METHOD_NAIVE_FLAT,
    METHOD_NAIVE_LOSER,
    VERDICT_FLAT,
    VERDICT_LLM_WINS,
    VERDICT_NAIVE_WINS,
    VERDICT_NOISE,
    VERDICT_TIE,
    choose_method,
    classify_backtest,
    is_flat_series,
)

# Реальні MAE з прогонів (DeepSeek, 8 точок).
CORE_CPI = (0.294, 0.758)          # LLM виграє з великим відривом
EUROZONE_HICP = (0.413, 0.363)     # LLM стабільно гірший
JAPAN_POLICY_RATE = (0.0314, 0.0315)  # "перемога" +0.4% — шум
UNEMPLOYMENT = (0.0625, 0.0625)    # нічия
FED_FUNDS = (0.0, 0.0)             # ряд не рухався


# ============================================================
# Класифікація прогону
# ============================================================


def test_big_win_is_llm_wins():
    verdict, advantage = classify_backtest(*CORE_CPI)
    assert verdict == VERDICT_LLM_WINS
    assert 60 < advantage < 62  # живе значення +61.3%


def test_loss_is_naive_wins():
    verdict, advantage = classify_backtest(*EUROZONE_HICP)
    assert verdict == VERDICT_NAIVE_WINS
    assert advantage < 0


def test_tiny_edge_is_noise_not_a_win():
    """+0.4% на 8 точках (`japan_policy_rate`) — не перевага: при
    зміні моделі такі "перемоги" перетворювались на поразки."""
    verdict, advantage = classify_backtest(*JAPAN_POLICY_RATE)
    assert verdict == VERDICT_NOISE
    assert 0 < advantage < config.FORECAST_MEANINGFUL_EDGE_PCT


def test_equal_mae_is_tie():
    assert classify_backtest(*UNEMPLOYMENT)[0] == VERDICT_TIE


def test_both_zero_is_flat_series_not_tie():
    """`fed_funds_rate`/`eurozone_deposit_rate`: обидва безпомилкові,
    бо ряд не рухався. Це "нема що прогнозувати", а не "нічия
    моделей" — і метод має бути інший."""
    assert classify_backtest(*FED_FUNDS)[0] == VERDICT_FLAT


def test_missing_mae_does_not_crash():
    assert classify_backtest(None, 1.0)[0] == VERDICT_TIE
    assert classify_backtest(1.0, None)[0] == VERDICT_TIE


# ============================================================
# Плоский ряд — детермінована перевірка ПЕРЕД будь-яким викликом
# ============================================================


def test_flat_series_detected():
    assert is_flat_series([2.0] * config.FORECAST_FLAT_SERIES_POINTS)


def test_moving_series_is_not_flat():
    values = [2.0] * (config.FORECAST_FLAT_SERIES_POINTS - 1) + [2.25]
    assert not is_flat_series(values)


def test_short_history_is_not_flat():
    """Замало точок — це "невідомо", не "не рухається": інакше новий
    показник із двома однаковими значеннями назавжди лишився б на
    naive."""
    assert not is_flat_series([2.0, 2.0])


# ============================================================
# Вибір методу
# ============================================================

MOVING = [100.0, 101.0, 103.0, 102.0, 105.0, 107.0, 110.0]
FLAT = [4.25] * 8


def test_flat_series_skips_llm_even_with_a_winning_verdict():
    """Перевірка плоского ряду йде ПЕРША: вона детермінована й
    безкоштовна, тож не має сенсу платити за виклик навіть там, де
    бектест колись показав перевагу."""
    choice = choose_method(FLAT, verdict=VERDICT_LLM_WINS, advantage_pct=61.3)
    assert choice.use_llm is False
    assert choice.method == METHOD_NAIVE_FLAT


def test_llm_used_when_backtest_proved_advantage():
    choice = choose_method(MOVING, verdict=VERDICT_LLM_WINS, advantage_pct=61.3,
                           verdict_model="deepseek-chat")
    assert choice.use_llm is True
    assert choice.method == METHOD_LLM
    assert "61.3" in choice.why


def test_llm_skipped_when_it_lost_to_naive():
    """Головна мета P0: 4 показники переставають платити за прогноз,
    гірший за безкоштовну базову лінію."""
    choice = choose_method(MOVING, verdict=VERDICT_NAIVE_WINS, advantage_pct=-13.8)
    assert choice.use_llm is False
    assert choice.method == METHOD_NAIVE_LOSER
    assert "ГІРШИЙ" in choice.why


def test_llm_skipped_on_noise_edge():
    choice = choose_method(MOVING, verdict=VERDICT_NOISE, advantage_pct=0.4)
    assert choice.use_llm is False
    assert "шуму" in choice.why


def test_llm_skipped_on_tie():
    choice = choose_method(MOVING, verdict=VERDICT_TIE, advantage_pct=0.0)
    assert choice.use_llm is False


def test_reason_is_always_human_readable():
    """`why` іде не лише в лог, а й у Telegram: користувач має бачити,
    ЧОМУ показник без ШІ-прогнозу, інакше це читається як збій."""
    for verdict in (VERDICT_LLM_WINS, VERDICT_NAIVE_WINS, VERDICT_TIE, VERDICT_NOISE, VERDICT_FLAT):
        choice = choose_method(MOVING, verdict=verdict, advantage_pct=5.0)
        assert choice.why and len(choice.why) > 10


def test_no_verdict_keeps_llm_but_marks_it_unvalidated():
    """Свідомий компроміс (config.FORECAST_LLM_WITHOUT_VERDICT): новий
    показник не має тихо втратити прогноз, але й не має виглядати
    перевіреним."""
    choice = choose_method(MOVING, verdict=None)
    assert choice.use_llm is config.FORECAST_LLM_WITHOUT_VERDICT
    if config.FORECAST_LLM_WITHOUT_VERDICT:
        assert choice.method == METHOD_LLM_UNVALIDATED
        assert "перевірений" in choice.why
