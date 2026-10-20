"""
Open-Meteo — прогноз погоди на 7 днів для товарних погодних індексів
(`weather/indices.py`), без ключа.

**Навіщо (2026-10-20).** "Прогноз погоди" для газу й "погода в
Бразилії/Вʼєтнамі" для кави стояли в `ASSET_FACTOR_GAPS`. Для газу
погода — головний драйвер попиту в опалювальний сезон; для кави —
заморозки (арабіка, червень-серпень) і опади (цвітіння, збір).

**Основне джерело**, бо віддає всі точки індексу ОДНИМ запитом і готові
добові max/min/опади за UTC-добами. Звірка — з MET Norway
(`met_norway_adapter.py`), незалежною метеослужбою.
"""

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord
from weather.indices import INDICES, DailyWeather, combine, horizon_days, point_value, require_full_horizon

logger = logging.getLogger(__name__)

API_URL = "https://api.open-meteo.com/v1/forecast"

METRICS = {metric_id: metric_id for metric_id in INDICES}


class OpenMeteoAdapter(BaseAdapter):
    source = "open_meteo"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in INDICES:
            raise ValueError(f"Невідомий погодний індекс {metric_id!r}. Доступні: {sorted(INDICES)}")
        self.metric_id = metric_id
        self.points, self.kind = INDICES[metric_id]
        self.session = session or requests.Session()

    def fetch(self, **_ignored) -> Any:
        params = {
            "latitude": ",".join(str(p.lat) for p in self.points),
            "longitude": ",".join(str(p.lon) for p in self.points),
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
            "timezone": "UTC",
            "forecast_days": "9",
        }
        response = self.session.get(API_URL, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def normalize(self, raw_response: Any, issue_date: Optional[date] = None) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        issue_date = issue_date or fetched_at.date()

        # Одна точка — обʼєкт, кілька — список обʼєктів.
        locations = raw_response if isinstance(raw_response, list) else [raw_response]
        if len(locations) != len(self.points):
            raise ValueError(
                f"Open-Meteo {self.metric_id}: {len(locations)} точок у відповіді "
                f"замість {len(self.points)}: {str(raw_response)[:300]!r}"
            )

        days = horizon_days(issue_date)
        per_point: dict[str, float] = {}
        payload: dict[str, list] = {}
        for point, location in zip(self.points, locations):
            daily = (location or {}).get("daily") or {}
            by_day = {
                date.fromisoformat(day): DailyWeather(date.fromisoformat(day), t_max, t_min, precip)
                for day, t_max, t_min, precip in zip(
                    daily.get("time", []),
                    daily.get("temperature_2m_max", []),
                    daily.get("temperature_2m_min", []),
                    daily.get("precipitation_sum", []),
                )
            }
            horizon = require_full_horizon("Open-Meteo", self.metric_id, point, by_day, days)
            per_point[point.name] = point_value(self.kind, horizon)
            payload[point.name] = [[str(d.day), d.t_max, d.t_min, d.precip] for d in horizon]

        return [NormalizedRecord(
            source=self.source,
            metric_id=self.metric_id,
            value=combine(self.kind, per_point),
            observed_at=issue_date,
            fetched_at=fetched_at,
            raw_payload={"kind": self.kind, "per_point": per_point, "daily": payload},
        )]
