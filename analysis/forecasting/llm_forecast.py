"""
LLM-прогноз наступного значення одного макропоказника — ЧИСТА частина
(промпт + розбір відповіді + перевірка правдоподібності), без SQL і без
мережі, тестується на голих списках чисел (той самий стиль, що
trend.py).

## Чому LLM, а не лінійна регресія (зміна напрямку, 2026-09-28)

Рішення користувача (docs/decisions.md, "Повний новий спек"): прогноз
має формувати ШІ на основі зібраних даних, а НЕ Python-модель
(`trend.py:linear_trend_forecast`). Це свідомий виняток із
analysis/CLAUDE.md "не використовуйте LLM для того, що можна порахувати
звичайною формулою" — власне рішення користувача саме для
прогнозування, не порушення правила.

`trend.py` при цьому НЕ видаляється: `naive_forecast()` лишається
базовою лінією, проти якої міряється будь-яка модель (analysis/CLAUDE.md
"Тестування прогнозних моделей"), а `linear_trend_forecast()` — другою
точкою порівняння в `backtest_llm.py`. Видалити їх означало б втратити
можливість сказати, чи LLM узагалі кращий за "нічого не змінилось".

## Що СВІДОМО не йде в промпт: ринкове очікування

`release_log.expected_value` (прогноз ForexFactory на наступний реліз)
у промпт НЕ передається, хоча технічно доступний. Дві причини:
1. Наш прогноз має бути ТРЕТЬОЮ незалежною точкою зору поруч із фактом
   і ринковим консенсусом (docs/archive/plan-journal-2026-10-11.md, Фаза 2) — заанкорений на консенсус
   прогноз такої цінності не має, він лише перекаже консенсус.
2. Backtest (`backtest_llm.py`) рахується на історії, де ринкового
   очікування на той момент у БД немає — промпт, що залежить від нього,
   був би неперевірюваним.
Порівняння "наш прогноз vs ринковий" робиться ПІЗНІШЕ, при видачі
(`reporting/forecast_notify.py`), де обидва числа вже відомі.

## Перевірка правдоподібності (is_plausible)

LLM легко плутає одиниці показника (рівень індексу CPI ~320 проти
річної зміни ~3.2%) або видає число іншого порядку. Такий прогноз
гірший за відсутній: він тихо потрапив би в Telegram як готовий
висновок. Тому ЧИСЛО від LLM проходить детермінований гейт перед
збереженням — той самий принцип "контроль коректності є властивістю
системи, не ручною роботою в сесії" (критичне правило 7, CLAUDE.md).
"""

import logging
from dataclasses import dataclass
from typing import Optional

from config import (
    FORECAST_DELTA_WINDOWS,
    FORECAST_PLAUSIBILITY_FACTOR,
    FORECAST_YIELD_CONTEXT_METRICS,
)

# ЯКА ВЕРСІЯ ПРОМПТА — у продакшені БЕЗ таблиці Δ.
#
# P1 плану покращення прогнозу (таблиця кроків Δ) реалізований,
# ЗМІРЯНИЙ і ВІДКОЧЕНИЙ 2026-10-06. Гіпотеза була розумна: обидві
# сталі слабкості (розвороти й ускорення) мають одне джерело — модель
# бачить лише рівні й мусить віднімати сама. A/B на 8 показниках ×
# 50 точок (той самий маршрут, ті самі точки, змінювалась ОДНА річ):
#   Δ допоміг 1 показнику, зашкодив 6, не змінив 1; у середньому
#   -2.9 п.п. Найгірше — `retail_sales` +7.9% -> -1.0% (перемога
#   стала поразкою) і `cpi` +43.2% -> +37.9%.
# Схоже, готові Δ відвертають модель від самого РІВНЯ, а саме рівень і
# є тим, на чому LLM виграє. Код варіанту лишається (`include_deltas`,
# `--delta` у бектесті) — щоб будь-хто міг перевірити це сам, а не
# вірити записові.
#
# Пишеться у `backtest_verdicts.prompt_variant`,
# щоб результати, зміряні різними промптами, не виглядали як один
# вимір (2026-10-06, двічі за день: спершу вікно історії, потім поява
# Δ — обидва рази таблиця ставала несумісною сама з собою, і це було
# видно лише за часом у лозі).
#
# ⚠️ ЗМІНИВ ПРОМПТ — ЗМІНИ ЦЕЙ РЯДОК. Інакше новий вимір перезапише
# старий вердикт, не сказавши, що умови були інші.
PROMPT_VARIANT = "levels"

# Назва варіанту З таблицею Δ — щоб бектест міг зміряти обидва на тих
# самих точках (`backtest_llm.py --no-delta`) і показати, чи Δ справді
# допомагає, а не просто "здається кращим".
PROMPT_VARIANT_WITH_DELTA = "levels+delta"

from llm_common import (
    DIRECTIONS,
    SynthesisResponseError,
    parse_confidence,
    parse_json_object,
)

logger = logging.getLogger(__name__)

# Поріг гейта правдоподібності — `config.py` (2026-10-06: був літералом
# тут, що порушувало правило "один конфіг на весь агент", і саме це
# значення відкинуло правильний за напрямком прогноз на COVID-обвалі;
# обґрунтування нового значення — у конфігу).
PLAUSIBILITY_FACTOR = FORECAST_PLAUSIBILITY_FACTOR

# Мінімум точок історії, щоб гейт мав на чому працювати: з 2 точок є
# лише один крок, і він же — міра "нормального" кроку, що робить
# перевірку безсенсовою (будь-який прогноз у межах 3 кроків пройде).
PLAUSIBILITY_MIN_HISTORY = 4

# ОБОВ'ЯЗКОВІ поля відповіді. `reasoning` свідомо НЕ тут (2026-10-04,
# живий кейс `unemployment_rate`): DeepSeek віддав коректний прогноз
# (forecast_value/direction/confidence/summary), але без `reasoning` —
# і вся точка backtest була втрачена через відсутнє поле для АУДИТУ.
# Аудит при цьому не страдає: повна сира відповідь однаково лежить у
# `llm_call_log` (rule 5, CLAUDE.md), тобто `reasoning` — зручність
# читання, а не єдине джерело обґрунтування. Відкидати робочий прогноз
# через нього — строгість не на користь справі.
FORECAST_FIELDS = ("forecast_value", "direction", "confidence", "summary")

SYSTEM_PROMPT = (
    "Ти макро-аналітик. Тобі дають історію одного макроекономічного "
    "показника — впорядкований за часом список (дата: значення), у тих "
    "самих одиницях, у яких показник публікує офіційне джерело."
    "\n\n"
    "Твоя задача — спрогнозувати, яким буде НАСТУПНЕ значення цього "
    "показника (наступний період публікації), і пояснити чому."
    "\n\n"
    "КРИТИЧНО про одиниці: прогноз має бути в ТИХ САМИХ одиницях і "
    "того самого порядку величини, що значення в наданій історії. Якщо "
    "історія — рівень індексу (напр. 322.1), прогнозуй рівень індексу, "
    "а НЕ річну зміну у відсотках. Якщо історія — відсоток (напр. 4.2), "
    "прогнозуй відсоток. Число іншого порядку буде відкинуто як "
    "помилкове."
    "\n\n"
    "Опирайся на те, що видно в самих даних: напрямок і крутизна "
    "останнього тренду, типовий розмір кроку між періодами, сезонність "
    "(якщо проглядається), розвороти, чи рух сповільнюється. Якщо дані "
    "не дають підстав для зміни — чесно прогнозуй значення, близьке до "
    "останнього, і скажи це в обґрунтуванні; вигадана динаміка гірша "
    "за визнану невизначеність."
    "\n\n"
    "НІКОЛИ не давай торгових рекомендацій ('купити'/'продати'/"
    "'входити в позицію') — тільки опис показника й очікуваної "
    "динаміки. Відповідай ЛИШЕ JSON-об'єктом з полями: forecast_value "
    "(ЧИСЛО — прогноз наступного значення, у тих самих одиницях, що "
    "історія), direction (одне з: up, down, neutral, unclear — куди "
    "показник рухається відносно останнього відомого значення), "
    "confidence (число від 0 до 1 — наскільки впевнений прогноз), "
    "summary (1-2 речення: яке значення очікується і чому), reasoning "
    "(коротке обґрунтування для аудиту: на яку саме динаміку в даних ти "
    "спирався), watch_on_release (1-2 речення: на ЩО САМЕ звернути "
    "увагу, коли показник вийде — яка складова чи деталь релізу "
    "покаже, чи прогноз виправдався, і що означатиме відхилення в "
    "кожен бік)."
)


@dataclass
class ForecastResult:
    """Вихід LLM-прогнозу. Своя форма (додає `forecast_value`), тому
    власний клас і власний парсер тут, а не
    llm_common.parse_synthesis_response() — той самий принцип, що
    expectations/synthesize.py:ExpectationSynthesisResult."""

    forecast_value: float
    direction: str
    confidence: float
    summary: str
    reasoning: str
    # "На що звернути увагу при виході" (2026-10-13, постановка
    # користувача 2026-10-11). Принцип агента вимагав цієї частини, а
    # в контракті відповіді її не було: прогноз давав ЧИСЛО й
    # обґрунтування, але не те, ЩО перевіряти в самому релізі.
    # Необов'язкове поле — з тієї самої причини, що `reasoning`
    # (2026-10-04): валідна числова відповідь не має гинути через
    # відсутність текстового поля, повна сира відповідь однаково в
    # `llm_call_log`.
    watch_on_release: str = ""


def parse_forecast_response(raw_content: str) -> ForecastResult:
    data = parse_json_object(raw_content, error_class=SynthesisResponseError)

    missing = [f for f in FORECAST_FIELDS if f not in data]
    if missing:
        raise SynthesisResponseError(f"У відповіді LLM бракує полів {missing}: {data!r}")

    try:
        forecast_value = float(data["forecast_value"])
    except (TypeError, ValueError) as e:
        raise SynthesisResponseError(
            f"forecast_value не число: {data['forecast_value']!r}"
        ) from e

    direction = data["direction"]
    if direction not in DIRECTIONS:
        raise SynthesisResponseError(f"Неочікуване значення direction: {direction!r}")

    reasoning = data.get("reasoning")
    if not reasoning:
        # Не помилка (див. FORECAST_FIELDS вище) — але лог потрібен,
        # щоб систематичне зникнення поля було видно, а не вгадувалось.
        logger.warning(
            "reasoning відсутнє у відповіді LLM — прогноз приймається, "
            "обґрунтування беремо з summary (повна відповідь — у llm_call_log)"
        )
        reasoning = data["summary"]

    return ForecastResult(
        forecast_value=forecast_value,
        direction=direction,
        confidence=parse_confidence(data["confidence"], error_class=SynthesisResponseError),
        summary=data["summary"],
        reasoning=reasoning,
        watch_on_release=(data.get("watch_on_release") or "").strip(),
    )


def is_plausible(values: list[float], forecast_value: float) -> tuple[bool, Optional[str]]:
    """(чи правдоподібний, причина відхилення). `values` — ХРОНОЛОГІЧНО
    (найстаріше перше), той самий порядок, що trend.py.

    Міра "нормального" кроку — НАЙБІЛЬШИЙ історичний |Δ| між сусідніми
    точками, а не середній: середній крок гладкої серії (CPI) близький
    до нуля, і будь-який осмислений прогноз відхилявся б від нього на
    багато "кроків". Максимум — найщедріша розумна межа, яку видно
    прямо в даних.

    Серія без жодного руху (усі кроки 0) гейт не проходить ніхто, крім
    точного повтору останнього значення — тому такий випадок свідомо
    пропускається (повертає True): це не помилка одиниць, а вироджена
    історія, і краще зберегти прогноз, ніж відкинути все."""
    if len(values) < PLAUSIBILITY_MIN_HISTORY:
        return True, None

    steps = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
    max_step = max(steps)
    if max_step == 0:
        return True, None

    last = values[-1]
    deviation = abs(forecast_value - last)
    limit = PLAUSIBILITY_FACTOR * max_step
    if deviation > limit:
        return False, (
            f"прогноз {forecast_value:.6g} відходить від останнього відомого "
            f"{last:.6g} на {deviation:.6g} — більше за {PLAUSIBILITY_FACTOR}× "
            f"найбільшого історичного кроку ({max_step:.6g}); схоже на помилку "
            f"одиниць/порядку, не на прогноз"
        )
    return True, None


def production_prompt_variant(metric_id: str) -> str:
    """Який варіант промпта використовує БОЙОВИЙ прогноз для цього
    показника.

    Потрібен, щоб вибір методу читав вердикт ТІЄЇ САМОЇ конфігурації
    (2026-10-11, живий дефект): `mortgage_rate_30y` на промпті
    `levels` дає −3.8% (ШІ гірший), а на `levels+yields` — +16.0%
    (ШІ кращий). Продакшен подає цьому показнику контекст дохідностей,
    тож читати вердикт без контексту означало б вимкнути ШІ на
    підставі виміру конфігурації, якої в продакшені немає."""
    if metric_id in FORECAST_YIELD_CONTEXT_METRICS:
        return f"{PROMPT_VARIANT}+yields"
    return PROMPT_VARIANT


def mean_delta(values: list[float], window: int) -> Optional[float]:
    """Середній КРОК (зміна до попередньої точки) за останні `window`
    кроків. None, якщо кроків менше, ніж window.

    Чиста функція — щоб зведення Δ у промпті було перевіряним без
    мережі (P1, 2026-10-06)."""
    if window < 1 or len(values) < window + 1:
        return None
    recent = values[-(window + 1):]
    deltas = [b - a for a, b in zip(recent, recent[1:])]
    return sum(deltas) / len(deltas)


def build_prompt(
    metric_id: str,
    label: str,
    observations_chronological: list[dict],
    include_deltas: bool = False,
    market_context: Optional[str] = None,
) -> str:
    """`observations_chronological` — найстаріше перше (як trend.py), з
    ключами observed_at/value; викликач відповідає за розворот
    fetch_recent().

    `include_deltas` — показувати КРОКИ (Δ) поруч із рівнями. ДЕФОЛТ
    False: ідея зміряна A/B і відкинута (докладно — коментар біля
    `PROMPT_VARIANT` вище). Параметр лишається, щоб гіпотезу можна
    було перевірити знову на інших даних чи іншій моделі, не
    переписуючи промпт.

    `market_context` — готовий текстовий блок ринкового контексту
    (`market_context.format_snapshot()`): дохідності 2Y/10Y і спред
    2s10s станом на ТУ САМУ дату, що й остання точка історії. Подається
    лише показникам із `config.FORECAST_YIELD_CONTEXT_METRICS` —
    2026-10-11, фідбек користувача про випереджаючий характер кривої.
    Йде ПЕРЕД історією: це фон, на якому читається сам ряд."""
    values = [float(obs["value"]) for obs in observations_chronological]

    lines = [
        f"Показник: {label} (metric_id={metric_id})",
        f"Кількість точок історії: {len(observations_chronological)}",
        "",
    ]
    if market_context:
        lines.append(market_context)
        lines.append("")
    lines += [
        "Історія (від найстарішого до найновішого"
        + ("; Δ — зміна до попередньої точки):" if include_deltas else "):"),
    ]
    previous: Optional[float] = None
    for obs, value in zip(observations_chronological, values):
        if previous is None or not include_deltas:
            lines.append(f"{obs['observed_at']}: {value:.6g}")
        else:
            lines.append(f"{obs['observed_at']}: {value:.6g}  (Δ {value - previous:+.6g})")
        previous = value

    summary = [] if not include_deltas else [
        f"за {window} кроків {delta:+.4g}"
        for window in FORECAST_DELTA_WINDOWS
        if (delta := mean_delta(values, window)) is not None
    ]
    if summary:
        lines.append("")
        lines.append("Середній крок (Δ): " + "; ".join(summary) + ".")

    lines.append("")
    lines.append(
        "Спрогнозуй наступне значення цього показника (наступний період "
        "публікації) у тих самих одиницях."
    )
    return "\n".join(lines)


# metric_id → опис для ПРОМПТА: що це за серія, в яких одиницях і з
# якою частотою публікується (docs/metrics-catalog.md). Свідомо окремо
# від reporting/telegram_notify.py:METRIC_LABELS — там підпис для
# ЛЮДИНИ в Telegram (короткий), тут контекст для МОДЕЛІ (одиниці й
# частота, без яких LLM плутає рівень індексу з річною зміною, і без
# яких "наступний період" неоднозначний: тиждень/місяць/квартал).
# analysis/ і reporting/ лишаються незалежними (жодного імпорту між
# ними) — той самий принцип, що reporting/CLAUDE.md.
METRIC_DESCRIPTIONS = {
    "cpi": "CPI США (CPIAUCSL) — РІВЕНЬ індексу споживчих цін, публікується щомісяця",
    "core_cpi": "Core CPI США (CPILFESL, без їжі/енергії) — РІВЕНЬ індексу, щомісяця",
    "pce_price_index": "PCE Price Index США (PCEPI, орієнтир ФРС) — РІВЕНЬ індексу, щомісяця",
    "fed_funds_rate": "Effective Fed Funds Rate (DFF) — ставка у відсотках, щодня",
    "treasury_10y": "Дохідність 10-річних US Treasuries (DGS10) — відсотки, щодня",
    "treasury_2y": "Дохідність 2-річних US Treasuries (DGS2) — відсотки, щодня",
    "unemployment_rate": "Рівень безробіття США (UNRATE) — відсотки, щомісяця",
    "nonfarm_payrolls": "Non-Farm Payrolls США (PAYEMS) — ЗАГАЛЬНА зайнятість у тисячах осіб (рівень, не приріст), щомісяця",
    "initial_jobless_claims": "Initial Jobless Claims США (ICSA) — кількість первинних заявок на допомогу, щотижня",
    "real_gdp": "Real GDP США (GDPC1) — РІВЕНЬ у млрд доларів 2017 року (chained), щокварталу",
    "retail_sales": "Retail Sales США (RSAFS) — РІВЕНЬ роздрібних продажів у млн доларів, щомісяця",
    "housing_starts": "Housing Starts США (HOUST) — початки будівництва, тисяч одиниць (SAAR), щомісяця",
    "mortgage_rate_30y": "30-річна фіксована ставка по іпотеці США (MORTGAGE30US) — відсотки, щотижня",
    "usdjpy_fx_rate": "Курс USD/JPY (DEXJPUS, Fed H.10) — єн за долар, щодня",
    "eurozone_hicp": "HICP єврозони — РІЧНА зміна у відсотках (annual rate of change), щомісяця",
    "eurozone_deposit_rate": "Deposit Facility Rate ЄЦБ — ставка у відсотках, щодня",
    "eurozone_unemployment_rate": "Рівень безробіття єврозони — відсотки, щомісяця",
    "japan_policy_rate": "Uncollateralized O/N Call Rate (політична ставка Банку Японії) — відсотки",
    "japan_cpi": "CPI Японії (e-Stat, база 2025=100) — РІВЕНЬ індексу, щомісяця",
}


def describe_metric(metric_id: str) -> str:
    """Опис показника для промпта. Невідомий metric_id — віддаємо сам
    id (прогноз усе одно можливий: історія в промпті несе одиниці
    неявно), не падаємо: перелік показників проєкту росте, і новий
    показник без опису має працювати гірше, а не ніяк."""
    return METRIC_DESCRIPTIONS.get(metric_id, metric_id)
