"""
Зведення ІСТОРІЇ OI/обсягу по кількох біржах в один денний ряд —
дзеркало того, що `aggregate_sources.py` робить із ЖИВИМ знімком
(сума по біржах, не середнє). Чиста функція (rule 1, CLAUDE.md): SQL —
`_history_db.py`, проводка — `run_screening.py`.

**Чому це взагалі окремий шар.** До 2026-10-15 історія читалась із
ОДНОЇ біржі (`run_screening.HISTORY_SOURCE = "bybit_futures"`), а
живий рівень `open_interest_usd` уже тоді був кросбіржовою сумою.
Наслідок зміряно: з 1056 символів ринку історію OI мали 855, обсягу —
855, решта отримувала `None` → `long_screen.py:63` і
`short_watch_screen.py:83` трактують це як "критерій не пройдено", тож
кожен шостий символ не міг стати ні LONG, ні SHORT **ніколи**. Дані
Binance/OKX при цьому збирались щодня й не читались.

**Чому "узгоджений набір бірж", а не просто сума всього, що є.**
Наївна сума по днях дала б ФАЛЬШИВИЙ рух там, де змінилось покриття, а
не потік: символ лежить на Bybit усі 8 днів, на 3-й день додається
лістинг OKX — сума стрибає на розмір OKX-частки, і скринінг прочитав
би це як приріст OI. Тому в розрахунок беруться лише біржі, присутні
І в найновішому, І в найстарішому дні вікна (`_endpoint_sources()`).
Так порівнюється однакове з однаковим, а symbol, що мав одне джерело,
поводиться точно як до зміни — гірше за старе поведінка стати не може.
"""

from typing import Optional


def _endpoint_sources(by_day: dict, days: list) -> set:
    """Біржі, присутні і в найновішому, і в найстарішому дні вікна."""
    return set(by_day[days[0]]) & set(by_day[days[-1]])


def consistent_daily_series(rows, limit: int) -> list[tuple]:
    """`rows` — (observed_at, source, value) у будь-якому порядку
    (результат `_history_db.fetch_history_rows()`).

    Повертає [(observed_at, value)] **найновіше перше**, не довше
    `limit`, де value — сума по біржах з узгодженого набору (docstring
    модуля). Порожній список, якщо вікно коротше за `limit` або
    спільних бірж на його краях немає — виклик трактує це так само, як
    трактував брак історії досі (`None` → критерій не пройдено).
    """
    by_day: dict = {}
    for observed_at, source, value in rows:
        by_day.setdefault(observed_at, {})[source] = value

    days = sorted(by_day, reverse=True)[:limit]
    if len(days) < limit:
        return []

    sources = _endpoint_sources(by_day, days)
    if not sources:
        return []

    series: list[tuple] = []
    for day in days:
        present = [v for s, v in by_day[day].items() if s in sources]
        if not present:
            # Жодної з узгоджених бірж у цей день — ряд рветься. Коротший
            # ряд виклик відкине так само, як брак історії: вигадувати
            # значення (інтерполяція/перенос) означало б показати рух,
            # якого ми не спостерігали.
            continue
        series.append((day, sum(present)))

    return series if len(series) == limit else []


def series_values(series: list[tuple]) -> list[float]:
    """Лише значення ряду, найновіше перше — у тому вигляді, в якому їх
    очікує арифметика `run_screening._pct_change()`."""
    return [float(value) for _day, value in series]


def oi_change_pct(series: list[tuple]) -> Optional[float]:
    """Зміна від найстарішого дня вікна до найновішого, у %."""
    values = series_values(series)
    if len(values) < 2:
        return None
    old, new = values[-1], values[0]
    if old == 0:
        return None
    return (new - old) / abs(old) * 100


def volume_spike_pct(series: list[tuple]) -> Optional[float]:
    """Найсвіжіший обсяг проти СЕРЕДНЬОГО за попередні дні вікна, у %."""
    values = series_values(series)
    if len(values) < 2:
        return None
    latest = values[0]
    baseline = values[1:]
    avg_baseline = sum(baseline) / len(baseline)
    if avg_baseline == 0:
        return None
    return (latest - avg_baseline) / abs(avg_baseline) * 100
