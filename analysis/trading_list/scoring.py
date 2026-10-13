"""
Скоринг торгового списку — ЧИСТІ функції, без SQL і без мережі
(той самий стиль, що `forecasting/trend.py` і
`crypto_screening/indicators.py`): тестуються на голих числах і
списках dict.

Три компоненти, кожен нормований у [0, 1], потім зважена сума
(`config.py:W_CATALYST/W_TREND/W_QUALITY`). Чому ОЦІНКА, а не бінарні
гейти на кожному критерії — `docs/trading-list.md`: бінарні дали б або
порожній список, або всі 215 рядків скринінгу; відсікання одне, на
фінальному порозі.

Усі пороги/ваги — виключно з `config.py`, тут жодного магічного числа.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

import config
from trading_list.categories import MACRO_DAMPING

# Напрямки. up/down/neutral/unclear — ті самі, що решта проєкту
# (llm_common.py:DIRECTIONS). CONFLICTING — власне значення саме цього
# модуля: компоненти вказують у протилежні боки. Це НЕ "unclear"
# (немає сигналу) — суперечність сама є інформацією, і вгадувати
# більшістю голосів було б гірше, ніж показати її користувачу
# (docs/trading-list.md).
DIRECTION_UP = "up"
DIRECTION_DOWN = "down"
DIRECTION_NEUTRAL = "neutral"
DIRECTION_UNCLEAR = "unclear"
DIRECTION_CONFLICTING = "conflicting"

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass
class CatalystHit:
    """Одна причина, чому актив у фокусі. `weight` — з config.py
    (CATALYST_WEIGHT_*), `direction` — куди вказує саме цей сигнал
    ('neutral', якщо напрямку немає — напр. запланований реліз, що
    ще не вийшов: подія відома, напрямок ні).

    `metric_id`/`detail`/`when` — СТРУКТУРОВАНІ поля для людського
    тексту в reporting/ (живий фідбек 2026-10-04: готовий склеєний
    рядок не давав reporting/ з чого зробити читабельне пояснення —
    ні перекласти metric_id, ні згрупувати той самий реліз).
    `text` лишається технічним аудит-слідом."""

    kind: str          # release_upcoming | release_done | news | synthesis | forecast
    weight: Decimal
    direction: str = DIRECTION_NEUTRAL
    text: str = ""
    metric_id: str = ""
    detail: str = ""
    when: str = ""

    def as_reason(self) -> dict:
        """Структурована форма для `trading_list.reasons` (JSONB)."""
        return {
            "kind": self.kind,
            "direction": self.direction,
            "metric_id": self.metric_id,
            "detail": self.detail,
            "when": self.when,
        }


@dataclass
class ScoreBreakdown:
    """Результат скорингу одного активу. Внески зберігаються ОКРЕМО
    (а не лише `total`), бо саме вони дають змогу калібрувати ваги —
    без них незрозуміло, що підняло актив у топ (db/schema.sql:
    trading_list, той самий урок, що diagnose_symbol.py)."""

    total: Decimal
    catalyst: Decimal
    trend: Decimal
    quality: Decimal
    direction: str
    catalyst_summary: str = ""
    # Структуровані причини для reporting/ (JSONB у trading_list).
    reasons: list[dict] = field(default_factory=list)
    # Напрямок і пояснення тренду окремо — щоб reporting/ міг сказати,
    # ЩО саме суперечить ("тренд вгору, але новини вниз"), а не лишати
    # марну позначку "суперечливо" без розшифровки (живий фідбек
    # 2026-10-04).
    trend_direction: str = DIRECTION_UNCLEAR
    trend_detail: str = ""


def _clamp(value: Decimal) -> Decimal:
    """Обрізає в [0, 1] — усі компоненти скору живуть у цьому діапазоні,
    щоб поріг `MIN_SCORE` читався як відсоток."""
    if value < _ZERO:
        return _ZERO
    return _ONE if value > _ONE else value


def pct_change(values: list[Decimal], periods: int) -> Optional[Decimal]:
    """%-зміна останнього значення відносно значення `periods` точок
    тому. `values` — ХРОНОЛОГІЧНО (найстаріше перше), той самий
    порядок, що forecasting/trend.py. None, якщо історії не досить або
    базове значення нульове (ділення на нуль)."""
    if len(values) <= periods or periods <= 0:
        return None
    base = values[-1 - periods]
    if base == _ZERO:
        return None
    return (values[-1] - base) / abs(base) * Decimal("100")


def trend_score(values: list[Decimal]) -> tuple[Decimal, str, str]:
    """(скор [0,1], напрямок, пояснення) з денних закриттів.

    Шар торгуємості для АКЦІЙ і WATCHLIST — у крипти свій уже є
    (RSI/OI/funding, crypto_screening/). Логіка: беремо %-зміну за
    коротке вікно як силу імпульсу, і довше вікно як перевірку, чи
    імпульс узгоджений із ширшим рухом. Узгоджені вікна дають повний
    внесок, розбіжні — половину: рух проти ширшого тренду для свінгу
    менш надійний, але не нульовий (це може бути початок розвороту).

    Нижче `TREND_MIN_CHANGE_PCT` — боковик, внесок 0. Вище
    `TREND_SATURATION_PCT` внесок більше не росте: інакше актив, що
    вже злетів у рази, автоматично займав би весь топ (той самий
    відомий пробіл, що в LONG-скрині крипти, docs/decisions.md
    2026-10-03)."""
    if len(values) < config.TREND_MIN_POINTS:
        return _ZERO, DIRECTION_UNCLEAR, (
            f"історії недосить для тренду ({len(values)} точок, "
            f"потрібно >= {config.TREND_MIN_POINTS})"
        )

    short = pct_change(values, config.TREND_SHORT_DAYS)
    if short is None:
        # Коротке вікно довше за наявну історію — беремо всю історію,
        # що є (краще слабший сигнал, ніж жодного: watchlist-актив,
        # доданий 3 тижні тому, інакше назавжди лишався б без тренду).
        short = pct_change(values, len(values) - 1)
    if short is None:
        return _ZERO, DIRECTION_UNCLEAR, "не вдалось порахувати зміну ціни"

    magnitude = abs(short)
    if magnitude < config.TREND_MIN_CHANGE_PCT:
        return _ZERO, DIRECTION_NEUTRAL, f"боковик ({short:+.2f}% за вікно)"

    span = config.TREND_SATURATION_PCT - config.TREND_MIN_CHANGE_PCT
    raw = _ONE if span <= _ZERO else (magnitude - config.TREND_MIN_CHANGE_PCT) / span
    raw = _clamp(raw)

    direction = DIRECTION_UP if short > _ZERO else DIRECTION_DOWN

    long = pct_change(values, config.TREND_LONG_DAYS)
    if long is None:
        detail = f"{short:+.2f}% за коротке вікно (довге вікно — історії недосить)"
    elif (long > _ZERO) == (short > _ZERO):
        detail = f"{short:+.2f}% коротке / {long:+.2f}% довге — узгоджені"
    else:
        raw = raw / Decimal("2")
        detail = f"{short:+.2f}% коротке / {long:+.2f}% довге — РОЗБІЖНІ"

    return raw, direction, detail


def crypto_trend_score(
    setup: str,
    oi_change_pct: Optional[Decimal] = None,
    pump_pct: Optional[Decimal] = None,
) -> tuple[Decimal, str, str]:
    """(скор [0,1], напрямок, пояснення) для крипто-кандидата.

    Окремо від `trend_score()` свідомо: крипта вже має власний шар
    торгуємості (`crypto_screening/`: ліквідність → RSI → funding →
    зміна OI), тож рахувати для неї ще й 20/60-денний тренд з денних
    закриттів означало б міряти те саме вдруге гіршим інструментом.
    Базовий внесок дається за САМ ФАКТ проходження власного скринінгу,
    далі модулюється магнітудою.

    Напрямок беремо зі статусу, не з ціни: `long` — продовження
    тренду вгору, `short`/`watch`/`candidate` — виснаження після
    пампу, тобто ризик вниз."""
    base = config.CRYPTO_SETUP_BASE_SCORE
    headroom = _ONE - base

    if setup == "long":
        direction = DIRECTION_UP
        magnitude, full = oi_change_pct, config.CRYPTO_FULL_OI_CHANGE_PCT
        label = "приріст OI"
    else:
        direction = DIRECTION_DOWN
        magnitude, full = pump_pct, config.CRYPTO_FULL_PUMP_PCT
        label = "памп"

    if magnitude is None or full <= _ZERO:
        return base, direction, f"сетап {setup} (магнітуда невідома)"

    extra = _clamp(abs(magnitude) / full) * headroom
    return (
        _clamp(base + extra),
        direction,
        f"сетап {setup}, {label} {magnitude:+.1f}%",
    )


def catalyst_score(hits: list[CatalystHit], is_stock: bool = False) -> tuple[Decimal, str]:
    """(скор [0,1], людський підсумок причин).

    Нормування: сума ваг спрацьованих сигналів діленням на
    `CATALYST_WEIGHT_RELEASE_HIGH * MACRO_DAMPING +
    CATALYST_WEIGHT_NEWS` — тобто "високоімпактний реліз ПЛЮС значима
    новина" і далі вважається повним каталізатором (1.0). Свідомо не
    сума ВСІХ можливих ваг: вимагати одночасно релізу, новини,
    синтезу й прогнозу означало б, що 1.0 недосяжна на практиці, і
    поріг втратив би сенс.

    `* MACRO_DAMPING` у знаменнику з'явився 2026-10-13 РАЗОМ із
    приглушенням макро для всіх класів активів — інакше стеля стала б
    недосяжною: без цієї правки реліз+новина давали (0.25+0.7)/1.7 =
    0.56, тобто "повний каталізатор" перестав би існувати, і всі
    пороги відбору поїхали б униз на 44%. Після правки знаменник
    0.95: реліз+новина = 1.0 як і було, власна новина сама = 0.74,
    голе макро = 0.26. Саме та пропорція, якої бракувало.

    **Широкі макро-сигнали приглушуються для ВСІХ класів активів**
    (`MACRO_DAMPING`, з 2026-10-13; раніше — лише для акцій).
    Макро-реліз стосується всього ринку одразу й НЕ різнить активи
    між собою: ні 215 тикерів, ні btc від eth від eurusd. Без
    приглушення глобальний high-реліз (0.59 після нормування) важив
    БІЛЬШЕ за власну новину про актив (0.41) — повна арифметика й
    живий наслідок у `categories.py:MACRO_DAMPING`.

    `is_stock` лишається параметром, бо впливає на інші частини
    скорингу, але на приглушення макро вже не впливає."""
    if not hits:
        return _ZERO, ""

    denominator = (
        config.CATALYST_WEIGHT_RELEASE_HIGH * Decimal(str(MACRO_DAMPING))
        + config.CATALYST_WEIGHT_NEWS
    )

    weighted: list[Decimal] = []
    parts: list[str] = []
    for hit in hits:
        weight = hit.weight
        # `startswith("release")`, не точний перелік: типи хітів
        # перейменовані на release_upcoming/release_done (2026-10-04,
        # структуровані причини для reporting/) — і точна перевірка
        # `in ("release", "forecast")` ТИХО перестала приглушувати
        # макро для акцій. Жива регресія: каталізатор акцій підскочив
        # 0.18 → 0.71, і 6 акцій заповнили список.
        if hit.kind.startswith("release") or hit.kind == "forecast":
            weight = weight * Decimal(str(MACRO_DAMPING))
        weighted.append(weight)
        if hit.text:
            parts.append(hit.text)

    # Зараховуються лише НАЙСИЛЬНІШІ CATALYST_MAX_COUNTED_HITS — жива
    # причина (прогін 2026-10-04): проста сума ваг давала компонент
    # 1.00 у половини активів, тобто він насичувався й перестав
    # РІЗНИТИ кандидатів, а різнити й було його задачею (активів із
    # 5-10 хітами багато: кілька релізів × кілька категорій кожного).
    # У ТЕКСТІ причин лишаються всі — для людини вони корисні.
    weighted.sort(reverse=True)
    total = sum(weighted[: config.CATALYST_MAX_COUNTED_HITS], _ZERO)

    score = _clamp(total / denominator) if denominator > _ZERO else _ZERO
    return score, "; ".join(parts)


def quality_score(composite: Optional[Decimal], best_composite: Optional[Decimal]) -> Decimal:
    """Нормований фундаментальний скор акції
    (`screening_results.score`) відносно НАЙЛУЧШОГО в цьому ж прогоні.

    Відносна, а не абсолютна нормалізація: абсолютна шкала composite
    не визначена (`screening/composite_score.py` рахує зважену суму
    зростання/оцінки, межі залежать від вибірки), тож єдине
    осмислене — місце серед тих, хто пройшов скринінг СЬОГОДНІ.

    Крипта/форекс/товари фундаменталу не мають → нейтральні 0.5, не 0:
    нуль був би ШТРАФОМ за відсутність даних, а не оцінкою
    (docs/trading-list.md)."""
    if composite is None or best_composite is None or best_composite <= _ZERO:
        return Decimal("0.5")
    return _clamp(composite / best_composite)


def resolve_direction(votes: list[str]) -> str:
    """Напрямок сигналу з голосів компонентів.

    Суперечність (є і 'up', і 'down') віддається як CONFLICTING, а НЕ
    розвʼязується більшістю — рішення дизайну: суперечність сама є
    корисним сигналом ("тренд вгору, але новина погана"), і вгадування
    приховало б її. `analysis/CLAUDE.md` однаково забороняє давати
    вказівку "купити/продати", тож список описує стан, а не велить
    діяти."""
    meaningful = [v for v in votes if v in (DIRECTION_UP, DIRECTION_DOWN)]
    if not meaningful:
        neutral = [v for v in votes if v == DIRECTION_NEUTRAL]
        return DIRECTION_NEUTRAL if neutral else DIRECTION_UNCLEAR

    has_up = DIRECTION_UP in meaningful
    has_down = DIRECTION_DOWN in meaningful
    if has_up and has_down:
        return DIRECTION_CONFLICTING
    return DIRECTION_UP if has_up else DIRECTION_DOWN


def combine(
    catalyst: Decimal,
    trend: Decimal,
    quality: Decimal,
    is_watchlist: bool = False,
) -> Decimal:
    """Зважена сума компонентів + бонус watchlist.

    Бонус додається ПІСЛЯ зважування й ПЕРЕД порогом — тобто пріоритет
    вибору користувача, але не безумовний пропуск: watchlist-актив без
    жодного каталізатора й без тренду бонусом поріг не переступить
    (0 + 0 + 0.5*0.20 + 0.10 = 0.20 < MIN_SCORE 0.35). Саме це
    відрізняє список від `/notify_watchlist`, який уже існує."""
    total = (
        catalyst * config.W_CATALYST
        + trend * config.W_TREND
        + quality * config.W_QUALITY
    )
    if is_watchlist:
        total += config.WATCHLIST_SCORE_BONUS
    return _clamp(total)


def score_asset(
    values: list[Decimal],
    hits: list[CatalystHit],
    composite: Optional[Decimal] = None,
    best_composite: Optional[Decimal] = None,
    is_watchlist: bool = False,
    is_stock: bool = False,
    extra_direction: Optional[str] = None,
) -> ScoreBreakdown:
    """Повний скоринг одного активу — єдина точка, де компоненти
    зводяться разом. `extra_direction` — готовий напрямок із власного
    сетапу (крипта: LONG → up, SHORT → down), який нема сенсу
    перераховувати тут."""
    c_score, c_summary = catalyst_score(hits, is_stock=is_stock)
    t_score, t_direction, t_detail = trend_score(values)
    q_score = quality_score(composite, best_composite)

    votes = [h.direction for h in hits]
    votes.append(t_direction)
    if extra_direction:
        votes.append(extra_direction)

    return ScoreBreakdown(
        total=combine(c_score, t_score, q_score, is_watchlist=is_watchlist),
        catalyst=c_score,
        trend=t_score,
        quality=q_score,
        direction=resolve_direction(votes),
        catalyst_summary=c_summary,
        reasons=[h.as_reason() for h in hits],
        trend_direction=t_direction,
        trend_detail=t_detail,
    )


def select_final(
    scored: list[tuple[str, ScoreBreakdown, bool]],
) -> list[tuple[str, ScoreBreakdown, bool]]:
    """Фінальний відбір: поріг → зарезервовані слоти watchlist → топ.

    `scored` — [(asset_id, breakdown, is_watchlist)]. Порядок роботи
    важливий: ПОРІГ застосовується ПЕРШИМ, до резервування слотів —
    інакше watchlist-актив без каталізатора займав би слот лише тому,
    що він у watchlist (саме та вироджена поведінка, якої дизайн
    уникає; слот краще лишити порожнім)."""
    eligible = [item for item in scored if item[1].total >= config.MIN_SCORE]
    eligible.sort(key=lambda item: item[1].total, reverse=True)

    # Індекси, а не порівняння значень: два різні активи теоретично
    # можуть мати ІДЕНТИЧНИЙ breakdown, і `item not in reserved` тоді
    # викинув би не той рядок.
    reserved_idx: list[int] = [
        i for i, item in enumerate(eligible) if item[2]
    ][: config.WATCHLIST_RESERVED_SLOTS]
    reserved_set = set(reserved_idx)

    remaining_slots = config.MAX_ITEMS - len(reserved_idx)
    rest_idx = [i for i in range(len(eligible)) if i not in reserved_set][:remaining_slots]

    chosen = sorted(reserved_set | set(rest_idx))
    final = [eligible[i] for i in chosen]
    final.sort(key=lambda item: item[1].total, reverse=True)
    return final
