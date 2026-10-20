"""
EIA (U.S. Energy Information Administration) API v2 — першоджерело
енергетичних даних США:
- щотижневі ЗАПАСИ нафти й газу (Weekly Petroleum Status Report,
  Weekly Natural Gas Storage Report);
- щомісячні дані STEO (Short-Term Energy Outlook): видобуток нафти
  OPEC і Саудівської Аравії, видобуток газу й експорт СПГ США.

**Навіщо (2026-10-20).** Для нафти й газу запаси — головний чинник
ПРОПОЗИЦІЇ; видобуток OPEC/Саудівської Аравії — числовий наслідок
рішень OPEC+ щодо квот; видобуток газу й експорт СПГ — пропозиція й
зовнішній попит на газ США. До цього дня все це стояло в
`ASSET_FACTOR_GAPS`.

**STEO змішує історію з прогнозом.** Той самий ряд іде на півтора року
вперед (живо: 2027-12 у відповіді). Прогноз EIA — не спостереження, тож
у `raw_observations` пишуться лише місяці ДО поточного; поточний і
майбутні відкидаються.

**Ключ.** `EIA_API_KEY` (безкоштовна реєстрація eia.gov/opendata). Без
нього — `DEMO_KEY` зі спільним лімітом на IP: живо 2026-10-20 він
вичерпався за кілька пробних запитів, тож для продакшену ключ потрібен.

**Одна повторна спроба на зависання.** Живо 2026-10-20: приблизно
кожен п'ятий запит до api.eia.gov висить 30 с без жодного байта (на
обох IPv4-адресах), решта відповідає за 0.8 с. Це не ліміт (повтор
його не лікує — правило проєкту), а збій з'єднання, тож одна коротка
повторна спроба на ТАЙМАУТ/обрив; HTTP-помилка не повторюється.

**Звірка** — не тут (адаптер нічого не інтерпретує):
`monitoring/crosscheck_factors.py`, пари в `config.FACTOR_SERIES`.

**EIA повертає помилку як JSON** (`{"error": {...}}`) — інколи з
HTTP 200. Читати це як "даних немає" — та сама пастка, що м'який
троттл GDELT, тому падаємо голосно.
"""

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

API_ROOT = "https://api.eia.gov/v2"
DEMO_KEY = "DEMO_KEY"

_TIMEOUT_SECONDS = 20
_ATTEMPTS = 2


@dataclass(frozen=True)
class EiaSeries:
    route: str
    facet: str          # "series" у тижневих датасетах, "seriesId" у STEO
    series_id: str
    frequency: str      # weekly | monthly
    expected_unit: str  # перевіряється: зміна одиниць зсунула б ряд у масштабі
    unit_field: str     # "units" у тижневих, "unit" у STEO
    history_only: bool  # STEO: відкидати поточний і майбутні місяці (прогноз)


METRICS: dict[str, EiaSeries] = {
    "us_crude_stocks": EiaSeries("petroleum/stoc/wstk", "series", "WCESTUS1", "weekly", "MBBL", "units", False),
    "us_natgas_storage": EiaSeries("natural-gas/stor/wkly", "series", "NW2_EPG0_SWO_R48_BCF", "weekly", "BCF", "units", False),
    "opec_crude_production": EiaSeries("steo", "seriesId", "COPR_OPEC", "monthly", "million barrels per day", "unit", True),
    "saudi_crude_production": EiaSeries("steo", "seriesId", "COPR_SA", "monthly", "million barrels per day", "unit", True),
    "us_natgas_production": EiaSeries("steo", "seriesId", "NGPRPUS", "monthly", "billion cubic feet per day", "unit", True),
    "us_lng_exports": EiaSeries("steo", "seriesId", "NGEXPUS_LNG", "monthly", "billion cubic feet per day", "unit", True),
}


def _parse_period(period: str) -> date:
    """"2026-10-09" (тиждень) або "2026-09" (місяць → перше число)."""
    if len(period) == 7:
        year, month = period.split("-")
        return date(int(year), int(month), 1)
    return date.fromisoformat(period)


class EiaAdapter(BaseAdapter):
    source = "eia"

    def __init__(self, metric_id: str, api_key: Optional[str] = None,
                 session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(f"Немає серії EIA для {metric_id!r}. Доступні: {sorted(METRICS)}")
        self.metric_id = metric_id
        self.spec = METRICS[metric_id]
        self.api_key = api_key or os.environ.get("EIA_API_KEY") or DEMO_KEY
        self.session = session or requests.Session()
        self._limit: Optional[int] = None

    def _get(self, params: dict) -> Any:
        url = f"{API_ROOT}/{self.spec.route}/data/"
        last_error: Optional[Exception] = None
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                response = self.session.get(url, params=params, timeout=_TIMEOUT_SECONDS)
                response.raise_for_status()
                return response.json()
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
                last_error = error
                logger.warning("EIA %s: спроба %d/%d — %s", self.spec.series_id, attempt, _ATTEMPTS, error)
        raise last_error

    def fetch(self, limit: Optional[int] = None, **_ignored) -> Any:
        if self.api_key == DEMO_KEY:
            logger.warning(
                "EIA: EIA_API_KEY не задано — працюю на DEMO_KEY (спільний "
                "ліміт на IP, вичерпується за кілька запитів). Безкоштовний "
                "ключ: eia.gov/opendata/register.php"
            )
        self._limit = limit
        # STEO віддає й прогноз на ~18 місяців уперед — запас на нього.
        length = (limit or 26) + (24 if self.spec.history_only else 0)
        params = {
            "api_key": self.api_key,
            "frequency": self.spec.frequency,
            "data[0]": "value",
            f"facets[{self.spec.facet}][]": self.spec.series_id,
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "length": str(length),
        }
        return self._get(params)

    def normalize(self, raw_response: Any, today: Optional[date] = None) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        current_month = (today or fetched_at.date()).replace(day=1)

        if not isinstance(raw_response, dict) or "error" in raw_response:
            raise ValueError(f"EIA {self.spec.series_id}: помилка API: {raw_response!r}"[:500])

        rows = (raw_response.get("response") or {}).get("data") or []
        if not rows:
            raise ValueError(f"EIA {self.spec.series_id}: у відповіді жодної точки")

        records: list[NormalizedRecord] = []
        for row in rows:
            if row.get(self.spec.facet) not in (None, self.spec.series_id):
                continue
            unit = row.get(self.spec.unit_field)
            if unit and unit != self.spec.expected_unit:
                raise ValueError(
                    f"EIA {self.spec.series_id}: одиниці {unit!r} замість "
                    f"{self.spec.expected_unit!r} — ряд зсунувся б у масштабі"
                )
            try:
                observed_at = _parse_period(row["period"])
                value = Decimal(str(row["value"]))
            except (KeyError, TypeError, ValueError, InvalidOperation):
                logger.warning("EIA %s: пропущено нерозбірний рядок %r", self.spec.series_id, row)
                continue
            if self.spec.history_only and observed_at >= current_month:
                continue
            records.append(NormalizedRecord(
                source=self.source,
                metric_id=self.metric_id,
                value=value,
                observed_at=observed_at,
                fetched_at=fetched_at,
                raw_payload={"series": self.spec.series_id, "unit": unit},
            ))

        if not records:
            raise ValueError(f"EIA {self.spec.series_id}: жодної придатної (історичної) точки")
        records.sort(key=lambda r: r.observed_at)
        return records[-self._limit:] if self._limit else records
