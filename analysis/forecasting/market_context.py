"""
РИНКОВИЙ КОНТЕКСТ для прогнозу — чисті функції (rule 1, CLAUDE.md):
ні БД, ні мережі. SQL — `_context_db.py`.

## Чому цей модуль існує

Прямий фідбек користувача 2026-10-11: "дохідності Treasury — це
важливий показник, він впливає на зміну ставки і є випереджаючим
індикатором; в дохідності облігацій взагалі багато логіки, яка
впливає на прогноз ставок".

До цього дохідності використовувались НЕ ЗА ПРИЗНАЧЕННЯМ: на них
робився точковий прогноз "яка дохідність буде завтра" (де переваги
над naive немає в жодному з 4 замірів — випадкове блукання на день
уперед), а прогнози, де вони справді несуть сигнал (ставка, інфляція),
їх НЕ БАЧИЛИ взагалі: `llm_forecast.build_prompt()` давав моделі лише
історію того самого показника.

Тобто найсильніший випереджаючий індикатор лежав у БД невикористаним.

## Що саме подається

- рівні 2Y і 10Y на дату прогнозу;
- зміна кожного за тиждень і за місяць (напрямок і швидкість руху
  очікувань по ставці);
- **спред 2s10s** — класичний сигнал: інверсія (10Y < 2Y) історично
  передує зниженню ставки й рецесії, звуження/розширення показує, куди
  зсуваються очікування.

## Критично: БЕЗ ЗАГЛЯДАННЯ В МАЙБУТНЄ

Контекст рахується станом на `as_of` — дату останньої точки історії,
яку бачить модель. У бектесті це дата перевіреної точки, не "сьогодні":
інакше прогноз на 2015 рік спирався б на дохідності 2026-го, і вся
виміряна точність була б фікцією. Перевіряється тестом
`test_market_context.py:test_context_never_uses_future_points`.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

# Скільки календарних днів назад брати для "зміни за тиждень/місяць".
# Календарні, не торгові: ряд дохідностей має пропуски (вихідні,
# свята), і найближча наявна точка в межах вікна — саме те, що
# потрібно, без власного календаря торгових днів.
WEEK_DAYS = 7
MONTH_DAYS = 30


@dataclass
class YieldSnapshot:
    """Стан кривої на дату. None у полі — даних на цю дату немає, і це
    чесно показується як "н/д", не підмінюється нулем."""

    short_value: Optional[float]
    long_value: Optional[float]
    short_week_change: Optional[float]
    long_week_change: Optional[float]
    short_month_change: Optional[float]
    long_month_change: Optional[float]

    @property
    def spread(self) -> Optional[float]:
        """2s10s: довга мінус коротка. Відʼємний = інверсія."""
        if self.short_value is None or self.long_value is None:
            return None
        return self.long_value - self.short_value


def value_as_of(series: list[tuple[date, float]], as_of: date) -> Optional[float]:
    """Останнє значення НЕ ПОЗДНІШЕ за `as_of`.

    `series` — відсортований за датою (найстаріше перше). Саме тут і
    тримається заборона на заглядання в майбутнє: усе, що після
    `as_of`, не існує для прогнозу."""
    result = None
    for point_date, value in series:
        if point_date > as_of:
            break
        result = value
    return result


def change_over(
    series: list[tuple[date, float]], as_of: date, days: int
) -> Optional[float]:
    """Зміна значення за останні `days` календарних днів станом на
    `as_of`. None, якщо немає однієї з двох точок."""
    current = value_as_of(series, as_of)
    earlier = value_as_of(series, date.fromordinal(as_of.toordinal() - days))
    if current is None or earlier is None:
        return None
    return current - earlier


def build_snapshot(
    short_series: list[tuple[date, float]],
    long_series: list[tuple[date, float]],
    as_of: date,
) -> YieldSnapshot:
    return YieldSnapshot(
        short_value=value_as_of(short_series, as_of),
        long_value=value_as_of(long_series, as_of),
        short_week_change=change_over(short_series, as_of, WEEK_DAYS),
        long_week_change=change_over(long_series, as_of, WEEK_DAYS),
        short_month_change=change_over(short_series, as_of, MONTH_DAYS),
        long_month_change=change_over(long_series, as_of, MONTH_DAYS),
    )


def format_snapshot(snapshot: YieldSnapshot) -> Optional[str]:
    """Текстовий блок для промпта. None, якщо немає жодного рівня —
    порожній блок лише заплутав би модель.

    Спред подається З ТРАКТУВАННЯМ ("інверсія" / "нормальна"), бо знак
    спреду і є тим сигналом, по якому читається напрямок очікувань;
    саму ІНТЕРПРЕТАЦІЮ (що це означає для показника) лишаємо моделі —
    тут лише факт, як і в решті промптів проєкту."""
    if snapshot.short_value is None and snapshot.long_value is None:
        return None

    def level(value: Optional[float], week: Optional[float], month: Optional[float]) -> str:
        if value is None:
            return "н/д"
        parts = [f"{value:.4g}%"]
        if week is not None:
            parts.append(f"за тиждень {week:+.2f}")
        if month is not None:
            parts.append(f"за місяць {month:+.2f}")
        return ", ".join(parts)

    lines = [
        "Ринковий контекст на цю саму дату (дохідності Treasury — "
        "випереджаючий індикатор очікувань по ставці):",
        f"- 2Y: {level(snapshot.short_value, snapshot.short_week_change, snapshot.short_month_change)}",
        f"- 10Y: {level(snapshot.long_value, snapshot.long_week_change, snapshot.long_month_change)}",
    ]
    spread = snapshot.spread
    if spread is not None:
        shape = "ІНВЕРСІЯ кривої" if spread < 0 else "нормальна крива"
        lines.append(f"- спред 10Y-2Y: {spread:+.2f} п.п. ({shape})")
    return "\n".join(lines)
