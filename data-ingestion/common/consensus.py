"""
Звірка значення одного показника між КІЛЬКОМА незалежними джерелами.

**Чому цього шару бракувало (2026-10-12, рішення користувача).** У БД
лежали ДВА джерела на Brent, що розходились на 33% (FRED 135.51 проти
TradingEconomics 102.25 за 2026-10-02), і НІЩО цього не ловило — обидва
рядки спокійно жили в `raw_observations`, а аналіз брав те, що
трапиться. Розбіжність виявив користувач очима, звіривши з реальним
терміналом; агент мовчав.

Critical rule 7 (CLAUDE.md) уже вимагає, щоб контроль коректності був
властивістю САМОЇ системи, а не ручною роботою в сесії, і вже дає
інструменти для ЧАСУ (`freshness.py` — старе/нове) і для ПЕРЕПИСУВАННЯ
(`quality.py` — те саме минуле значення саме змінилось). Бракувало
третього: **чи два незалежні джерела взагалі згодні, скільки це
значення**. Freshness і quality обидва сказали б "усе добре" про
FRED-ціну 135.51 — вона свіжа й ніколи не переписувалась. Вона просто
НЕПРАВИЛЬНА.

**Принцип, сформульований користувачем:** "корекні дані це онлайн
пошук і звірка на декількох джерелах". Тобто правда не в тому, що
джерело офіційне, а в тому, що кілька незалежних джерел кажуть те
саме.

**Що робить цей модуль:** приймає значення з кількох джерел на один
`metric_id` і повертає вердикт — узгоджені / є викид / даних
недосить. НЕ пише в БД і НЕ ходить у мережу (та сама межа, що в
`adapter.py:normalize`), тому тестується на звичайних числах.

**Чому медіана, а не "довіряти найофіційнішому":** живий кейс прямо
показав, що офіційність не корелює з правильністю — FRED (офіційне
першоджерело) був викидом, а TradingEconomics (скрейпінг) і ручна
звірка по двох сайтах збіглися. Медіана трьох і більше джерел стійка
до одного брехуна; для двох джерел вердикт — лише "згодні чи ні",
без вибору переможця.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

AGREE = "agree"
OUTLIER = "outlier"
INSUFFICIENT = "insufficient"
DISAGREE = "disagree"


@dataclass
class SourceValue:
    source: str
    value: Decimal


@dataclass
class ConsensusVerdict:
    metric_id: str
    status: str
    consensus: Optional[Decimal] = None
    max_spread_pct: Optional[Decimal] = None
    outliers: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def usable(self) -> bool:
        """Чи можна брати `consensus` в аналіз. `OUTLIER` придатний
        СВІДОМО: якщо з трьох джерел двоє згодні, а третє бреше, у нас
        є правильне значення — саме це й сталось із Brent. Непридатне
        лише те, де згоди немає взагалі."""
        return self.status in (AGREE, OUTLIER)


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


def _pct_diff(value: Decimal, reference: Decimal) -> Decimal:
    if reference == 0:
        return Decimal(0)
    return abs(value - reference) / abs(reference) * Decimal(100)


def reconcile(
    metric_id: str,
    values: list[SourceValue],
    tolerance_pct: Decimal,
) -> ConsensusVerdict:
    """Вердикт по одному показнику.

    `tolerance_pct` — скільки відсотків розбіжності вважаємо нормою
    (різні біржі/контракти/час зрізу дають природний розкид; живий
    замір Yahoo↔TradingEconomics — 0.3-1.4%). Пороги —
    `config.CONSENSUS_TOLERANCE_PCT`.
    """
    present = [sv for sv in values if sv.value is not None]

    if len(present) < 2:
        return ConsensusVerdict(
            metric_id=metric_id,
            status=INSUFFICIENT,
            consensus=present[0].value if present else None,
            detail=(
                f"лише {len(present)} джерело — звірити ні з чим "
                f"(потрібно 2+)"
            ),
        )

    median = _median([sv.value for sv in present])
    deviations = {sv.source: _pct_diff(sv.value, median) for sv in present}
    max_spread = max(deviations.values())

    agreeing = [s for s, d in deviations.items() if d <= tolerance_pct]
    outliers = [s for s, d in deviations.items() if d > tolerance_pct]

    if not outliers:
        return ConsensusVerdict(
            metric_id=metric_id,
            status=AGREE,
            consensus=median,
            max_spread_pct=max_spread,
            detail=(
                f"{len(present)} джерел узгоджені в межах "
                f"{tolerance_pct}% (максимальний розкид {max_spread:.2f}%)"
            ),
        )

    # Є викид. Чи лишилась більшість, на яку можна спертись?
    if len(agreeing) >= 2:
        trusted = [sv.value for sv in present if sv.source in agreeing]
        trusted_median = _median(trusted)
        logger.warning(
            "%s: джерело(а) %s розходяться з рештою понад %s%% — "
            "беремо медіану узгоджених (%s)",
            metric_id, ", ".join(sorted(outliers)), tolerance_pct, trusted_median,
        )
        return ConsensusVerdict(
            metric_id=metric_id,
            status=OUTLIER,
            consensus=trusted_median,
            max_spread_pct=max_spread,
            outliers=sorted(outliers),
            detail=(
                f"викид: {', '.join(sorted(outliers))} "
                f"(розкид {max_spread:.2f}%); узгоджені: "
                f"{', '.join(sorted(agreeing))}"
            ),
        )

    # Двоє джерел, і вони не згодні — переможця визначити нічим.
    logger.error(
        "%s: джерела не згодні й більшості немає (%s) — значення НЕ придатне",
        metric_id,
        "; ".join(f"{sv.source}={sv.value}" for sv in present),
    )
    return ConsensusVerdict(
        metric_id=metric_id,
        status=DISAGREE,
        consensus=None,
        max_spread_pct=max_spread,
        outliers=sorted(deviations, key=lambda s: -deviations[s]),
        detail=(
            f"розкид {max_spread:.2f}% понад допуск {tolerance_pct}%, "
            f"більшості немає: "
            + "; ".join(f"{sv.source}={sv.value}" for sv in present)
        ),
    )
