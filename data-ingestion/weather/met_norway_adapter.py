"""
MET Norway Locationforecast 2.0 — ДРУГЕ незалежне джерело прогнозу
погоди для звірки погодних індексів з Open-Meteo (`weather/indices.py`,
`config.FACTOR_SERIES`). Норвезька метеослужба, глобальне покриття,
без ключа.

**User-Agent обовʼязковий** (умови сервісу: інакше 403) і має містити
контакт. Береться з `MET_NORWAY_USER_AGENT`, а якщо його немає — з
`SEC_EDGAR_USER_AGENT` (там уже "назва + email" для того самого
призначення: щоб постачальник даних знав, хто звертається).

**Як рахуються добові величини.** Прогноз — це часовий ряд: спершу
щогодинний (~2.5 доби), далі 6-годинний (до ~9 діб). У кожної точки о
00/06/12/18 UTC є вікно `next_6_hours` з air_temperature_max/min і
опадами — чотири такі вікна РІВНО покривають UTC-добу. Тому беруться
лише вони: так немає подвійного рахунку перекритих щогодинних вікон, і
доба та сама, що в Open-Meteo (`timezone=UTC`).
"""

import logging
import os
import time
from datetime import date, datetime, timezone
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord
from weather.indices import INDICES, DailyWeather, combine, horizon_days, point_value, require_full_horizon

logger = logging.getLogger(__name__)

API_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"

METRICS = {metric_id: metric_id for metric_id in INDICES}

_WINDOW_HOURS = (0, 6, 12, 18)


def daily_from_timeseries(timeseries: list[dict]) -> dict[date, DailyWeather]:
    """UTC-доба → max/min/опади з чотирьох 6-годинних вікон. Доба, де
    бракує хоч одного вікна, лишається з None — `require_full_horizon`
    тоді голосно відмовить, а не порахує неповну суму."""
    windows: dict[date, list[dict]] = {}
    for entry in timeseries:
        moment = datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
        if moment.minute or moment.hour not in _WINDOW_HOURS:
            continue
        details = (entry.get("data", {}).get("next_6_hours") or {}).get("details") or {}
        if not details:
            continue
        windows.setdefault(moment.date(), []).append(details)

    result: dict[date, DailyWeather] = {}
    for day, parts in windows.items():
        if len(parts) != len(_WINDOW_HOURS):
            result[day] = DailyWeather(day, None, None, None)
            continue
        try:
            result[day] = DailyWeather(
                day,
                max(p["air_temperature_max"] for p in parts),
                min(p["air_temperature_min"] for p in parts),
                sum(p["precipitation_amount"] for p in parts),
            )
        except KeyError:
            result[day] = DailyWeather(day, None, None, None)
    return result


class MetNorwayAdapter(BaseAdapter):
    source = "met_norway"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None,
                 user_agent: Optional[str] = None):
        if metric_id not in INDICES:
            raise ValueError(f"Невідомий погодний індекс {metric_id!r}. Доступні: {sorted(INDICES)}")
        self.metric_id = metric_id
        self.points, self.kind = INDICES[metric_id]
        self.session = session or requests.Session()
        self.user_agent = (
            user_agent
            or os.environ.get("MET_NORWAY_USER_AGENT")
            or os.environ.get("SEC_EDGAR_USER_AGENT")
        )

    def fetch(self, **_ignored) -> Any:
        if not self.user_agent:
            raise RuntimeError(
                "MET Norway вимагає User-Agent із контактом: задайте "
                "MET_NORWAY_USER_AGENT (або SEC_EDGAR_USER_AGENT) у .env"
            )
        responses = []
        for index, point in enumerate(self.points):
            if index:
                time.sleep(0.5)
            response = self.session.get(
                API_URL,
                params={"lat": f"{point.lat:.2f}", "lon": f"{point.lon:.2f}"},
                headers={"User-Agent": self.user_agent},
                timeout=30,
            )
            response.raise_for_status()
            responses.append(response.json())
        return responses

    def normalize(self, raw_response: Any, issue_date: Optional[date] = None) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        issue_date = issue_date or fetched_at.date()

        if not isinstance(raw_response, list) or len(raw_response) != len(self.points):
            raise ValueError(f"MET Norway {self.metric_id}: очікувався список із {len(self.points)} відповідей")

        days = horizon_days(issue_date)
        per_point: dict[str, float] = {}
        payload: dict[str, list] = {}
        for point, body in zip(self.points, raw_response):
            timeseries = ((body or {}).get("properties") or {}).get("timeseries") or []
            by_day = daily_from_timeseries(timeseries)
            horizon = require_full_horizon("MET Norway", self.metric_id, point, by_day, days)
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
