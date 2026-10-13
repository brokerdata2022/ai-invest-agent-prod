"""
Арифметика пошуку новачків — ЧИСТІ функції (rule 1, CLAUDE.md): ні БД,
ні мережі, ні магічних чисел (усі пороги приходять параметрами з
`config.py`).

Ключова відмінність від `trading_list/scoring.py`: там питання "чи це
варто ТОРГУВАТИ" (каталізатор + тренд + якість), тут — "чи тут узагалі
щось відбувається, на що варто подивитись". Тому й сигналів лише три:
рух ціни, спайк обсягу, підтвердженість новинами.

**Напрямок — рівноправно в обидві сторони** (рішення користувача
2026-10-05): рух -15% це такий самий кандидат, як +15%. Напрямок
описує РУХ, не вказівку купити/продати (analysis/CLAUDE.md:
заборонені формулювання).
"""

from dataclasses import dataclass, field
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Optional

ONE = Decimal("1")
ZERO = Decimal("0")


@dataclass
class Candidate:
    """Один знайдений актив. `signals` — сирі числа, з яких вийшов
    score (для калібрування порогів на живих даних, не для показу)."""

    asset_id: str          # тикер акції / символ ф'ючерса — канонічний ідентифікатор
    label: str             # назва компанії або сам символ, якщо назви немає
    asset_class: str       # stock | crypto
    detector: str          # crypto_movers | stock_movers | news_tickers
    direction: str         # up | down | neutral | unclear
    reason: str            # ЧОМУ потрапив — людською мовою, детерміновано
    signals: dict = field(default_factory=dict)
    score: Decimal = ZERO
    source_refs: list = field(default_factory=list)
    # Заповнюється лише детектором, що справді викликав LLM
    # (`news_tickers`) — аудит-лог "чому агент так вирішив" (rule 5,
    # CLAUDE.md). Для детермінованих детекторів лишається None: там
    # жодного LLM-виклику немає.
    llm_call_id: Optional[int] = None


def pct_change(old: Optional[Decimal], new: Optional[Decimal]) -> Optional[Decimal]:
    """Зміна у відсотках. None (а не 0) при відсутніх/нульових даних —
    "невідомо" і "не змінилось" це різні речі, і скор мусить їх
    розрізняти."""
    if old is None or new is None:
        return None
    try:
        if Decimal(old) == ZERO:
            return None
        return (Decimal(new) - Decimal(old)) / Decimal(old) * Decimal("100")
    except (InvalidOperation, DivisionByZero, TypeError):
        return None


def direction_from_move(move_pct: Optional[Decimal]) -> str:
    """Напрямок РУХУ (не рекомендація). `unclear` при невідомому
    русі — той самий словник напрямків, що решта проєкту
    (db/schema.sql: up | down | neutral | unclear)."""
    if move_pct is None:
        return "unclear"
    if move_pct > ZERO:
        return "up"
    if move_pct < ZERO:
        return "down"
    return "neutral"


def volume_spike_pct(
    volumes: list[Decimal], window: Optional[int] = None
) -> Optional[Decimal]:
    """Обсяг останнього дня проти СЕРЕДНЬОГО за попередні `window`
    днів (None = уся наявна історія). Спільна для обох
    movers-детекторів: подвоєння обсягу означає те саме і для акції, і
    для ф'ючерса.

    Менше 3 точок — None ("невідомо"), не 0: база з одного дня дала б
    випадковий сигнал на першому ж тихому дні."""
    if len(volumes) < 3:
        return None
    span = len(volumes) - 1 if window is None else window
    if span < 2 or len(volumes) < span + 1:
        return None
    baseline_window = volumes[-(span + 1) : -1]
    baseline = sum(baseline_window) / Decimal(len(baseline_window))
    return pct_change(baseline, volumes[-1])


def max_daily_move_pct(closes: list[Decimal]) -> Optional[Decimal]:
    """Найбільша ЗА МОДУЛЕМ зміна між двома сусідніми точками ряду.

    Потрібна, щоб відрізнити подію на ринку від КОРПОРАТИВНОЇ ДІЇ:
    спліт/спін-оф зсуває ціну одним стрибком на 33-50%+, і в ряду без
    коригування це виглядає як обвал (живий кейс CTVA, 2026-10-05 —
    docs/decisions.md). Рух, розтягнутий на кілька днів, такого
    стрибка не дає."""
    if len(closes) < 2:
        return None
    moves = [
        move
        for move in (pct_change(a, b) for a, b in zip(closes, closes[1:]))
        if move is not None
    ]
    if not moves:
        return None
    return max(moves, key=abs)


def saturating_ratio(value: Optional[Decimal], reference: Decimal) -> Decimal:
    """|value| / reference, обрізане на 1.0. Насичення обов'язкове:
    без нього один памп на +300% давав би скор у кілька разів вищий за
    решту й витісняв усіх інших кандидатів зі списку, хоча "дивись
    негайно" вже й так сказано на +40%."""
    if value is None or reference <= ZERO:
        return ZERO
    ratio = abs(Decimal(value)) / reference
    return ratio if ratio < ONE else ONE


def score_candidate(
    move_pct: Optional[Decimal],
    move_saturation_pct: Decimal,
    w_move: Decimal,
    w_volume: Decimal,
    w_news: Decimal,
    volume_spike_pct: Optional[Decimal] = None,
    volume_reference_pct: Decimal = Decimal("100"),
    news_source_count: int = 0,
    news_source_reference: int = 4,
) -> Decimal:
    """Зважена сила сигналу, 0..1.

    Компонент, за яким даних немає, дає 0 — не викидає кандидата й не
    підставляє "середнє". Наслідок свідомий: кандидат лише з рухом
    ціни (найчастіший випадок) отримає максимум `w_move`. Саме тому
    скор тут РАНЖУЄ, а не допускає: гейт допуску — детермінований
    поріг руху в самому детекторі, `DISCOVERY_MIN_SCORE` лишається
    санітарним мінімумом (config.py, розділ 7)."""
    move = saturating_ratio(move_pct, move_saturation_pct)
    volume = saturating_ratio(volume_spike_pct, volume_reference_pct)
    news = saturating_ratio(
        Decimal(news_source_count), Decimal(news_source_reference)
    )
    return (w_move * move + w_volume * volume + w_news * news).quantize(
        Decimal("0.0001")
    )


def select_top(
    candidates: list[Candidate],
    min_score: Decimal,
    max_items: int,
    max_per_detector: int,
) -> list[Candidate]:
    """Фінальний відбір: поріг скору → квота на детектор → загальний
    ліміт.

    Квота на детектор (а не просто "топ-N за скором") — бо крипта
    завжди дає найбільше й найсильніше рухів, і без квоти список
    новачків перетворився б на другий крипто-скринінг, а акції й
    новинні тикери не з'являлись би ніколи."""
    eligible = [c for c in candidates if c.score >= min_score]
    eligible.sort(key=lambda c: c.score, reverse=True)

    per_detector: dict[str, int] = {}
    selected: list[Candidate] = []
    for candidate in eligible:
        if len(selected) >= max_items:
            break
        used = per_detector.get(candidate.detector, 0)
        if used >= max_per_detector:
            continue
        per_detector[candidate.detector] = used + 1
        selected.append(candidate)
    return selected
