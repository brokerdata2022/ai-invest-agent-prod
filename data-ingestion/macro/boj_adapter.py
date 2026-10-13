"""
Адаптер Bank of Japan Time-Series Data Search API — офіційне
першоджерело для Policy Rate Японії. https://www.stat-search.boj.or.jp/

Третій адаптер проєкту (Фаза 1, після FRED і ECB). Ключ не потрібен —
API відкритий для будь-кого без реєстрації (на відміну від FRED і
e-Stat).

Підтверджено живим прогоном користувача 2026-08-30 (5 записів
japan_policy_rate успішно збережено в raw_observations) — структура
JSON-відповіді (GET_STATS → DATA_INF → DATA_OBJ → SURVEY_DATES/VALUES)
відповідає документації, normalize() підтвердив свою захисну логіку
пошуку на реальних даних.
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

BOJ_API_URL = "https://www.stat-search.boj.or.jp/api/v1/getDataCode"

METRICS: dict[str, tuple[str, str]] = {
    "japan_policy_rate": ("FM01", "STRDCLUCON"),
}

_DATE_FORMATS = ("%Y%m%d", "%Y%m")


def _parse_survey_date(value: str) -> Optional[date]:
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _find_series_objects(node: Any) -> list[dict]:
    found: list[dict] = []
    if isinstance(node, dict):
        if "SURVEY_DATES" in node and "VALUES" in node:
            found.append(node)
        else:
            for value in node.values():
                found.extend(_find_series_objects(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_series_objects(item))
    return found


class BojAdapter(BaseAdapter):
    source = "boj"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(
                f"Невідомий metric_id для BOJ: {metric_id!r}. "
                f"Доступні: {sorted(METRICS)}"
            )
        self.metric_id = metric_id
        self.db_name, self.series_code = METRICS[metric_id]
        self.session = session or requests.Session()

    def fetch(
        self,
        limit: Optional[int] = None,
        observation_start: Optional[str] = None,
        observation_end: Optional[str] = None,
    ) -> Any:
        params: dict[str, Any] = {
            "format": "json",
            "lang": "en",
            "db": self.db_name,
            "code": self.series_code,
        }
        if observation_start:
            params["startDate"] = observation_start
        if observation_end:
            params["endDate"] = observation_end

        response = self.session.get(BOJ_API_URL, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def normalize(self, raw_response: Any, limit: Optional[int] = None) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        records: list[NormalizedRecord] = []

        series_objects = _find_series_objects(raw_response)
        if not series_objects:
            logger.warning(
                "Не знайдено серій із SURVEY_DATES/VALUES у відповіді BOJ для %s "
                "(db=%s, code=%s) — можливо, змінилась структура відповіді, "
                "перевірте вручну.",
                self.metric_id, self.db_name, self.series_code,
            )
            return records

        for series in series_objects:
            dates = series.get("SURVEY_DATES") or []
            values = series.get("VALUES") or []
            for raw_date, raw_value in zip(dates, values):
                if raw_value in (None, "", "null"):
                    continue
                try:
                    value = Decimal(str(raw_value))
                except InvalidOperation:
                    logger.warning(
                        "Не вдалось розпарсити значення BOJ %s за %s: %r",
                        self.series_code, raw_date, raw_value,
                    )
                    continue

                observed_at = _parse_survey_date(str(raw_date))
                if observed_at is None:
                    continue

                records.append(
                    NormalizedRecord(
                        source=self.source,
                        metric_id=self.metric_id,
                        value=value,
                        observed_at=observed_at,
                        fetched_at=fetched_at,
                        revision=None,
                        raw_payload={"SURVEY_DATE": raw_date, "VALUE": raw_value},
                    )
                )

        records.sort(key=lambda r: r.observed_at)
        if limit:
            records = records[-limit:]
        return records

    def collect(self, **kwargs) -> list[NormalizedRecord]:
        limit = kwargs.pop("limit", None)
        raw = self.fetch(**kwargs)
        return self.normalize(raw, limit=limit)
