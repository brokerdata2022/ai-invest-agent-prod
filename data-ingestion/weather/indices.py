"""
Погодні індекси для товарів: ДЕ дивитись і ЯК згорнути добові
прогнози в одне число. Спільне для обох погодних адаптерів
(`open_meteo_adapter.py`, `met_norway_adapter.py`), щоб два джерела
рахували РІВНО ту саму величину — інакше звірка порівнювала б різне.

**Чому згортання тут, а не в analysis/** (критичне правило 1). HDD —
стандартна одиниця галузі (NOAA: HDD = 65°F − (Tmax+Tmin)/2), тобто
нормалізація температури, а не оцінка "добре/погано". Зберігати
натомість у `raw_observations` сирі прогнози по кожному місту на кожен
день наперед означало б два різні "коли" в одному рядку (дата прогнозу
й дата, на яку він) — семантика `observed_at` цього не витримує. Сирі
добові значення по кожній точці лягають у `raw_payload` для аудиту.

`observed_at` індексу = дата ВИПУСКУ прогнозу; горизонт — завтра +6
днів (UTC-доби): сьогоднішня доба в MET Norway вже частково минула, а
в Open-Meteo — ні, і порівняння було б нечесним.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class Point:
    name: str
    lat: float
    lon: float


# Північний схід і Середній захід США — там основний опалювальний
# попит на газ узимку. Рівні ваги: зважування за населенням дало б
# точніше число, але не змінило б НАПРЯМОК (холодніше/тепліше), який
# і потрібен аналізу.
US_GAS_HEATING_CITIES = (
    Point("New York", 40.71, -74.01),
    Point("Chicago", 41.88, -87.63),
    Point("Boston", 42.36, -71.06),
    Point("Philadelphia", 39.95, -75.17),
    Point("Detroit", 42.33, -83.05),
    Point("Minneapolis", 44.98, -93.27),
    Point("Pittsburgh", 40.44, -80.00),
    Point("Washington", 38.91, -77.04),
)

# Арабіка: три головні регіони Мінас-Жерайс.
BRAZIL_COFFEE_POINTS = (
    Point("Varginha (Sul de Minas)", -21.55, -45.43),
    Point("Patrocinio (Cerrado Mineiro)", -18.94, -46.99),
    Point("Franca (Alta Mogiana)", -20.54, -47.40),
)

# Робуста: Центральне нагір'я, провінція Даклак.
VIETNAM_COFFEE_POINTS = (
    Point("Buon Ma Thuot (Dak Lak)", 12.67, 108.04),
)

HDD = "hdd_65f_sum"
MIN_TEMP = "min_temp_c"
PRECIP = "precip_sum_mm"

HORIZON_DAYS = 7

# metric_id → (точки, тип згортання)
INDICES: dict[str, tuple[tuple[Point, ...], str]] = {
    "us_hdd_7d": (US_GAS_HEATING_CITIES, HDD),
    "brazil_coffee_min_temp_7d": (BRAZIL_COFFEE_POINTS, MIN_TEMP),
    "brazil_coffee_precip_7d": (BRAZIL_COFFEE_POINTS, PRECIP),
    "vietnam_coffee_precip_7d": (VIETNAM_COFFEE_POINTS, PRECIP),
}


@dataclass
class DailyWeather:
    """Добові значення в одній точці (°C, мм)."""

    day: date
    t_max: Optional[float]
    t_min: Optional[float]
    precip: Optional[float]


def horizon_days(issue_date: date) -> list[date]:
    return [issue_date + timedelta(days=offset) for offset in range(1, HORIZON_DAYS + 1)]


def _hdd(t_max: float, t_min: float) -> float:
    mean_f = (t_max + t_min) / 2 * 9 / 5 + 32
    return max(0.0, 65.0 - mean_f)


def point_value(kind: str, days: list[DailyWeather]) -> float:
    """Згортка горизонту в одній точці. Порожні добові значення —
    помилка (горизонт мусить бути повним), а не "пропустити": неповна
    сума опадів читалась би як суха погода."""
    if kind == HDD:
        return sum(_hdd(d.t_max, d.t_min) for d in days)
    if kind == MIN_TEMP:
        return min(d.t_min for d in days)
    if kind == PRECIP:
        return sum(d.precip for d in days)
    raise ValueError(f"Невідомий тип погодного індексу {kind!r}")


def combine(kind: str, per_point: dict[str, float]) -> Decimal:
    """Точки → одне число. Мінімальна температура — НАЙГІРША точка
    (заморозок в одному регіоні вже б'є по врожаю), решта — середнє."""
    values = list(per_point.values())
    if kind == MIN_TEMP:
        result = min(values)
    else:
        result = sum(values) / len(values)
    return Decimal(str(round(result, 1)))


def require_full_horizon(source: str, metric_id: str, point: Point,
                         by_day: dict[date, DailyWeather], days: list[date]) -> list[DailyWeather]:
    missing = [
        d for d in days
        if d not in by_day or None in (by_day[d].t_max, by_day[d].t_min, by_day[d].precip)
    ]
    if missing:
        raise ValueError(
            f"{source} {metric_id}: у точці {point.name} неповний прогноз на "
            f"{', '.join(str(d) for d in missing)}"
        )
    return [by_day[d] for d in days]
