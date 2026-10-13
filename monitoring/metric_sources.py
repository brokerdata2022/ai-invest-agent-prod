"""
Реєстр "який адаптер даних відповідає metric_id" — спільний для
`refresh_calendar.py` (потрібен лише `.source`, для release_log) і
`check_releases.py` (потрібен клас адаптера + чи потрібен api_key, для
реального забору даних після настання часу релізу).

FRED-метрики беруться з `release_calendar.RELEASE_IDS` (одне джерело
істини, не дублюємо перелік удруге). Eurozone/Japan — без FRED-якоря
(докладніше `economic_calendar.py`), тому `CALENDAR_ONLY_METRICS`:
для них economic_calendar.py — ЄДИНЕ джерело дати релізу, не тільки
збагачення.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data-ingestion"))

from macro.boj_adapter import BojAdapter  # noqa: E402
from macro.ecb_adapter import EcbAdapter  # noqa: E402
from macro.estat_adapter import EstatAdapter  # noqa: E402
from macro.fred_adapter import DAILY_MACRO_METRICS, FredAdapter  # noqa: E402

from release_calendar import RELEASE_IDS  # noqa: E402

# metric_id → (клас адаптера, назва env-змінної з ключем, або None
# якщо ключ не потрібен).
ADAPTER_BY_METRIC: dict[str, tuple[type, str | None]] = {
    metric_id: (FredAdapter, "FRED_API_KEY") for metric_id in RELEASE_IDS
}
ADAPTER_BY_METRIC.update({
    "eurozone_hicp": (EcbAdapter, None),
    "eurozone_deposit_rate": (EcbAdapter, None),
    "eurozone_unemployment_rate": (EcbAdapter, None),
    "japan_policy_rate": (BojAdapter, None),
    "japan_cpi": (EstatAdapter, "ESTAT_APP_ID"),
    # Рішення FOMC по ставці — ДОДАНО 2026-10-11. `fed_funds_rate`
    # лишається й у DAILY_ADAPTER_BY_METRIC нижче (сам ряд
    # оновлюється щодня і щодня збирається), але тепер має ще й
    # КАЛЕНДАРНИЙ рядок: дату засідання. Доти її не було ніде, тому
    # прогноз робився "на завтра" — а щоденна ефективна ставка між
    # засіданнями стоїть на місці, звідси й вердикт "нічия" в
    # бектесті: прогнозувалось не те, що змінюється.
    "fed_funds_rate": (FredAdapter, "FRED_API_KEY"),
})

# ДЕННІ серії без жодного календаря релізів (не плутати з
# CALENDAR_ONLY_METRICS нижче — там календар Є, просто не FRED-ний).
# СВІДОМО окремий словник, а НЕ додавання в ADAPTER_BY_METRIC вище:
# `refresh_calendar.py:126` робить `sorted(ADAPTER_BY_METRIC)` і
# намагається завести календарний рядок для КОЖНОГО ключа — для денних
# серій це означало б вічне "немає збігу в economic_calendar" на
# кожному тижневому прогоні. Потрібен цей перелік для двох речей:
# щоденного збору (orchestration/jobs.py:_macro_daily_series) і
# прогнозу за розкладом (analysis/forecasting/forecast_daily.py) —
# 2026-10-04, рішення користувача "облігації це обовязково".
DAILY_ADAPTER_BY_METRIC: dict[str, tuple[type, str | None]] = {
    metric_id: (FredAdapter, "FRED_API_KEY") for metric_id in DAILY_MACRO_METRICS
}

# Показники без FRED release-календаря — economic_calendar.py дає
# дату напряму (find_upcoming_event()), не тільки збагачує вже відому
# FRED-дату (find_event()).
CALENDAR_ONLY_METRICS: list[str] = [
    # fed_funds_rate — американський показник, але календар-only
    # СВІДОМО (2026-10-11): FRED `/release/dates` для H.15 дає
    # ЩОДЕННІ дати публікації, а нам потрібна дата ЗАСІДАННЯ. Її знає
    # лише фід подій ("Federal Funds Rate" у
    # economic_calendar.TITLE_PATTERNS). Тобто FRED-якір тут не просто
    # зайвий — він давав би хибну дату щодня.
    "fed_funds_rate",
    "eurozone_hicp",
    "eurozone_deposit_rate",
    "eurozone_unemployment_rate",
    "japan_policy_rate",
    "japan_cpi",
]


# Макропоказники, для яких існує ДРУГЕ незалежне джерело (BLS) і які
# тому можна звіряти — `macro/bls_adapter.py:METRICS`. Решта 15
# показників лишається без звірки: real_gdp/pce — це BEA,
# retail_sales/housing_starts — Census, claims — DOL, і для них
# другого джерела ще не підключено (чесна межа, задокументована в
# docs/decisions.md 2026-10-13).
CROSSCHECKABLE_METRICS = ("cpi", "core_cpi", "unemployment_rate", "nonfarm_payrolls")
