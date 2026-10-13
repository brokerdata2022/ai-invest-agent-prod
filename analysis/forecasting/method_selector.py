"""
ЯКИМ методом прогнозувати конкретний показник — чисті функції
(rule 1, CLAUDE.md): ні БД, ні мережі, ні магічних чисел (пороги з
`config.py`, розділ 10).

## Чому цей модуль існує

До 2026-10-05 усі 19 показників отримували LLM-прогноз однаково. Три
бектести (DeepSeek / Opus 5 / Sonnet 5, таблиці в docs/archive/plan-journal-2026-10-11.md Фаза 2)
показали, що це не питання моделі:

- 6 показників LLM виграє з великим відривом на ВСІХ трьох моделях —
  це РІВНІ з інерцією (індекси цін, ВВП, будівництво);
- 4 програють naive на всіх трьох — продиференційовані ряди й
  випадкові блукання (`eurozone_hicp` це річна ЗМІНА, дохідності
  Treasury, тижневі claims);
- 2 ряди не рухались жодного разу за вікно — будь-який виклик там
  це витрата за прогноз "як вчора";
- 4 нестабільні: розкид МІЖ моделями більший за перевагу (до 60 п.п.),
  тобто на 8 точках вердикт не визначений.

Отже метод має обиратись за ПОКАЗНИКОМ і за ЗМІРЯНИМ результатом, а
не однаково для всіх.

## Порядок перевірок (саме такий)

1. **Плоский ряд** — детерміновано, без БД і без бектесту. Найдешевша
   й найнадійніша перевірка, тому перша.
2. **Вердикт бектесту** для моделі, якою прогноз РЕАЛЬНО робиться.
   Перевага менша за `FORECAST_MEANINGFUL_EDGE_PCT` — не перевага
   (три прогони показали, що такі "перемоги" при зміні моделі
   перетворюються на поразки).
3. **Немає вердикту** — `FORECAST_LLM_WITHOUT_VERDICT` (свідомо LLM,
   але з явним позначенням `llm_unvalidated`, не замовчуванням).
"""

from dataclasses import dataclass
from typing import Optional

from config import (
    FORECAST_FLAT_SERIES_POINTS,
    FORECAST_LLM_WITHOUT_VERDICT,
    FORECAST_MEANINGFUL_EDGE_PCT,
)

# Методи, що пишуться в `metric_forecasts.method`.
METHOD_LLM = "llm"
METHOD_LLM_UNVALIDATED = "llm_unvalidated"
METHOD_NAIVE_FLAT = "naive_flat"
METHOD_NAIVE_LOSER = "naive_llm_worse"

# Вердикти, що пишуться в `backtest_verdicts.verdict`.
VERDICT_LLM_WINS = "llm_wins"
VERDICT_NAIVE_WINS = "naive_wins"
VERDICT_TIE = "tie"
VERDICT_FLAT = "flat_series"
VERDICT_NOISE = "noise"


@dataclass
class MethodChoice:
    """`use_llm` — чи взагалі робити виклик; `method` — що піде в
    `metric_forecasts.method`; `why` — людською мовою для лога й для
    Telegram (користувач має бачити, ЧОМУ показник без ШІ-прогнозу,
    інакше це виглядає як збій)."""

    use_llm: bool
    method: str
    why: str


def is_flat_series(values: list[float], points: int = FORECAST_FLAT_SERIES_POINTS) -> bool:
    """Останні `points` значень однакові. Замало історії — False
    ("невідомо" не те саме, що "не рухається")."""
    if len(values) < points:
        return False
    window = values[-points:]
    return all(v == window[0] for v in window)


def classify_backtest(
    llm_mae: Optional[float],
    naive_mae: Optional[float],
    edge_pct: float = FORECAST_MEANINGFUL_EDGE_PCT,
) -> tuple[str, Optional[float]]:
    """Вердикт одного прогону бектесту + перевага у відсотках.

    Повертає `(verdict, advantage_pct)`, де advantage_pct > 0 означає
    "LLM точніший". Межа перемоги/шуму — `edge_pct`: саме через неї
    `japan_policy_rate` (+0.4%) не вважається перемогою, хоч формально
    MAE менший."""
    if llm_mae is None or naive_mae is None:
        return VERDICT_TIE, None

    if naive_mae == 0 and llm_mae == 0:
        # Обидва безпомилкові — ряд не рухався (fed_funds_rate,
        # eurozone_deposit_rate). Це не "нічия моделей", а "нема що
        # прогнозувати", і метод має бути інший.
        return VERDICT_FLAT, None
    if naive_mae == 0:
        return VERDICT_NAIVE_WINS, None

    advantage = (naive_mae - llm_mae) / naive_mae * 100
    if llm_mae == naive_mae:
        return VERDICT_TIE, 0.0
    if advantage >= edge_pct:
        return VERDICT_LLM_WINS, advantage
    if advantage > 0:
        return VERDICT_NOISE, advantage
    return VERDICT_NAIVE_WINS, advantage


def choose_method(
    values: list[float],
    verdict: Optional[str],
    advantage_pct: Optional[float] = None,
    verdict_model: Optional[str] = None,
) -> MethodChoice:
    """Який метод застосувати до показника ЗАРАЗ.

    `values` — історія в хронологічному порядку; `verdict` —
    збережений вердикт бектесту для моделі, якою прогноз реально
    робиться (None = не міряли)."""
    if is_flat_series(values):
        return MethodChoice(
            use_llm=False,
            method=METHOD_NAIVE_FLAT,
            why=(
                f"ряд не змінювався останні {FORECAST_FLAT_SERIES_POINTS} точок — "
                "прогноз дорівнює поточному значенню, виклик ШІ не потрібен"
            ),
        )

    if verdict is None:
        if FORECAST_LLM_WITHOUT_VERDICT:
            return MethodChoice(
                use_llm=True,
                method=METHOD_LLM_UNVALIDATED,
                why="бектест на цьому показнику ще не проводився — прогноз НЕ перевірений",
            )
        return MethodChoice(
            use_llm=False,
            method=METHOD_NAIVE_LOSER,
            why="бектест не проводився, а без доказу переваги ШІ не викликається",
        )

    if verdict == VERDICT_FLAT:
        return MethodChoice(
            use_llm=False,
            method=METHOD_NAIVE_FLAT,
            why="за бектестом ряд не рухається — прогноз дорівнює поточному значенню",
        )

    if verdict == VERDICT_LLM_WINS:
        edge = f" на {advantage_pct:.1f}%" if advantage_pct is not None else ""
        model = f" ({verdict_model})" if verdict_model else ""
        return MethodChoice(
            use_llm=True,
            method=METHOD_LLM,
            why=f"бектест{model}: ШІ точніший за базову лінію{edge}",
        )

    # naive_wins / tie / noise — усі три означають те саме рішення:
    # платити за виклик нема підстав. Але причина в тексті різна, бо
    # користувач має бачити різницю між "ШІ гірший" і "перевага в
    # межах шуму".
    reasons = {
        VERDICT_NAIVE_WINS: "бектест: ШІ ГІРШИЙ за базову лінію",
        VERDICT_TIE: "бектест: ШІ й базова лінія однаково точні",
        VERDICT_NOISE: "бектест: перевага ШІ в межах шуму",
    }
    detail = reasons.get(verdict, f"бектест: вердикт {verdict!r}")
    edge = f" ({advantage_pct:+.1f}%)" if advantage_pct is not None else ""
    model = f" ({verdict_model})" if verdict_model else ""
    return MethodChoice(
        use_llm=False,
        method=METHOD_NAIVE_LOSER,
        why=f"{detail}{edge}{model} — прогноз = поточне значення",
    )
