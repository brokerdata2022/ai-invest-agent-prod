"""
Приведення "сирого" значення показника (raw_observations) і ринкового
прогнозу (release_log.expected_value) до ОДНАКОВИХ одиниць, щоб їх
можна було відняти одне від одного.

Чому це взагалі потрібно (не тривіальний diff) — для частини
показників сире значення це РІВЕНЬ (індекс/сума), а прогноз
ForexFactory-фіду — m/m %, q/q % (annualized) чи y/y % відносно цього
рівня; для інших сире значення вже ставка/% і порівнюється напряму.
Джерело для кожного випадку — той самий адаптер, що збирає значення
(data-ingestion/macro/*_adapter.py), перевірено вручну під час
дизайну цієї фічі (docs/decisions.md).

metric_id-список дубльований з `monitoring/metric_sources.ADAPTER_BY_METRIC`
навмисно, без прямого імпорту — той самий принцип контейнерної
незалежності, що `reporting/telegram_notify.py:_METRIC_SOURCE`.
"""

from typing import Optional

# --- Метод порівняння для кожного metric_id, що має календар релізів. ---
#
# level_direct         — сире значення вже в тих самих одиницях, що прогноз
#                         (ставка/%, чи рівень, що прогноз дає напряму).
# mom_pct              — прогноз це m/m % зміни рівня, рахуємо з 2 останніх точок.
# diff_level           — прогноз це АБСОЛЮТНА зміна рівня (не %), 2 останні точки.
# qoq_pct_annualized   — прогноз це q/q % зміни у річному вимірі, 2 останні квартали.
# yoy_pct              — прогноз це y/y % зміни, точка зараз проти ~12 місяців тому.
COMPARISON_METHOD: dict[str, str] = {
    # --- США (FRED) ---
    "cpi": "mom_pct",                        # CPIAUCSL — рівень індексу
    "core_cpi": "mom_pct",                   # CPILFESL — рівень індексу
    "pce_price_index": "mom_pct",            # PCEPI — рівень індексу
    "nonfarm_payrolls": "diff_level",        # PAYEMS — рівень (тис. осіб)
    "unemployment_rate": "level_direct",     # UNRATE — уже %
    "real_gdp": "qoq_pct_annualized",        # GDPC1 — рівень (млрд $)
    "retail_sales": "mom_pct",               # RSAFS — рівень ($ млн)
    "housing_starts": "level_direct",        # HOUST — рівень (тис. одиниць SAAR), пряме порівняння
    "initial_jobless_claims": "level_direct",  # ICSA — рівень (осіб), пряме порівняння
    "mortgage_rate_30y": "level_direct",     # MORTGAGE30US — уже %; форекс-календар цей показник
                                              # не покриває (немає патерна в economic_calendar.py) —
                                              # expected_value завжди None, порівняння завжди пропускається.

    # --- Єврозона (ECB) ---
    "eurozone_hicp": "level_direct",         # ECB зберігає HICP одразу як YoY-annual-rate (ANR), не індекс
    "eurozone_deposit_rate": "level_direct", # уже %
    "eurozone_unemployment_rate": "level_direct",  # уже %

    # --- Японія (BOJ/e-Stat) ---
    "japan_policy_rate": "level_direct",     # уже %
    "japan_cpi": "yoy_pct",                  # e-Stat — обрано рівень індексу (estat_adapter.py), не %
}

# Масштаб, на який множиться СПАРСЕНИЙ прогноз (parse_expected_value),
# щоб збігтись з одиницями сирого ряду — за замовчуванням 1.0 (без
# перетворення). Потрібен лише там, де форекс-текст парситься в ПОВНІ
# одиниці, а сирий ряд FRED вимірюється в тисячах.
RAW_UNIT_SCALE: dict[str, float] = {
    "nonfarm_payrolls": 0.001,  # "180K" -> 180000 повних одиниць; PAYEMS уже в тисячах -> /1000
    "housing_starts": 0.001,    # "1.35M" -> 1_350_000; HOUST уже в тисячах одиниць -> /1000
}

# Скільки останніх спостережень (найновіше перше, як повертає
# common/db.py:fetch_recent) потрібно методу.
REQUIRED_OBSERVATIONS: dict[str, int] = {
    "level_direct": 1,
    "diff_level": 2,
    "mom_pct": 2,
    "qoq_pct_annualized": 2,
    # Наближення: 13-та за лічбою місячна точка ряду ~= 12 місяців тому.
    # Не перевіряє фактичний місяць/рік — якщо в ряду є пропуск, зсунеться.
    # Задокументована спрощеність (docs/decisions.md), той самий стиль
    # чесності, що срібло-проксі (docs/decisions.md).
    "yoy_pct": 13,
}


def compute_actual(method: str, observations: list[dict]) -> Optional[float]:
    """observations — результат `common/db.py:fetch_recent()`, найновіше
    перше. Повертає None, якщо спостережень бракує (виклик має
    перевірити len() >= REQUIRED_OBSERVATIONS[method] заздалегідь, це —
    страховка)."""
    required = REQUIRED_OBSERVATIONS.get(method)
    if required is None:
        raise ValueError(f"Невідомий метод порівняння: {method!r}")
    if len(observations) < required:
        return None

    latest = float(observations[0]["value"])

    if method == "level_direct":
        return latest

    if method == "diff_level":
        previous = float(observations[1]["value"])
        return latest - previous

    if method == "mom_pct":
        previous = float(observations[1]["value"])
        if previous == 0:
            return None
        return (latest - previous) / previous * 100

    if method == "qoq_pct_annualized":
        previous = float(observations[1]["value"])
        if previous == 0:
            return None
        return ((latest / previous) ** 4 - 1) * 100

    if method == "yoy_pct":
        year_ago = float(observations[required - 1]["value"])
        if year_ago == 0:
            return None
        return (latest / year_ago - 1) * 100

# ============================================================
# ТРИ ЧИСЛА ПОРУЧ: прогноз агента в одиницях порівняння
# ============================================================

CLOSER = "closer_than_calendar"
FARTHER = "farther_than_calendar"
TIE = "tie"


def forecast_in_comparison_units(
    method: str, forecast_value: float, observations: list[dict]
) -> Optional[float]:
    """Прогноз агента, переведений у ТІ САМІ одиниці, що `actual_value`.

    **Навіщо.** Прогноз лежить у `metric_forecasts` РІВНЕМ (cpi 334.9),
    а порівняння з ринковим очікуванням — у тих одиницях, якими ринок
    квотує показник (m/m % для cpi). Без переведення три числа поруч
    незрівнянні.

    **Як.** Тим самим `compute_actual()`, що порахував факт — прогноз
    підставляється на місце НАЙНОВІШОГО спостереження, а історія
    (`observations[1:]`) лишається та сама. Це не паралельна
    арифметика, а той самий код: інакше рано чи пізно формули
    розійшлись би, і «агент проти ринку» порівнювало б різні величини
    (той самий урок, що `feedback_measurement_must_match_production`).

    `observations` — як повертає `common/db.py:fetch_recent()`,
    найновіше перше, вже з РЕЛІЗНИМ значенням на позиції 0.
    """
    if not observations:
        return None
    substituted = [{"value": forecast_value}] + list(observations[1:])
    return compute_actual(method, substituted)


def compare_to_calendar(
    actual: float, forecast: Optional[float], expected: float
) -> Optional[str]:
    """Хто був ближчий до факту — наш прогноз чи прогноз із календаря.

    «Прогноз із календаря», а не «консенсус» (рішення користувача
    2026-10-15: «консенсус не ясно»): значення приходить із
    економічного календаря (`monitoring/economic_calendar.py`,
    ForexFactory-фід), і назвати його тим, чим воно є, зрозуміліше за
    термін, який треба знати заздалегідь.

    `None`, якщо прогнозу не було: це НЕ «агент програв», це
    відсутність твердження, і плутати їх означає псувати статистику
    (той самий принцип, що `no_claim` у журналі наслідків).
    """
    if forecast is None:
        return None
    agent_error = abs(actual - forecast)
    market_error = abs(actual - expected)

    # Порівняння з допуском, а не `==`. Живий приклад із тесту:
    # |1.0 - 0.9| = 0.09999999999999998, а |1.1 - 1.0| =
    # 0.10000000000000009 — математично рівні помилки, але через
    # подвійну точність "нічия" не визначалась би майже ніколи, а
    # вибір "ближче чи далі" вирішував шум останнього біта.
    # Допуск відносний, бо показники різного масштабу: m/m % (0.6) і
    # рівень зайнятості (158 000).
    scale = max(1.0, abs(agent_error), abs(market_error))
    if abs(agent_error - market_error) <= 1e-9 * scale:
        return TIE
    return CLOSER if agent_error < market_error else FARTHER
