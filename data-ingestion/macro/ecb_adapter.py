"""
Адаптер ECB Data Portal (колишній SDW — Statistical Data Warehouse,
API-ендпоінти перенаправлені на data-api.ecb.europa.eu без зміни
структури серій) — офіційне першоджерело для макропоказників єврозони.
https://data.ecb.europa.eu/help/api/overview

Другий адаптер проєкту (Фаза 1, після FRED). API-ключ не потрібен —
дані відкриті без реєстрації, на відміну від FRED.
"""

import csv
import io
import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

ECB_DATA_URL = "https://data-api.ecb.europa.eu/service/data"

# Внутрішній metric_id → (flowRef, seriesKey). ECB (на відміну від FRED)
# ідентифікує ряд парою "потік даних" + "ключ серії" (SDMX), тому тут
# кортеж, а не один рядок. Тримаємо явним словником з тієї ж причини,
# що й METRICS у fred_adapter.py — стабільний внутрішній metric_id
# незалежно від того, як саме влаштований SDMX-ключ у ECB.
METRICS: dict[str, tuple[str, str]] = {
    # HICP, євроarea (changing composition), річна зміна, %.
    # УВАГА: датасет ICP (старий) був офіційно закритий ECB 4 лютого
    # 2026 і замінений датасетом HICP (нова методологія Eurostat —
    # COICOP v2, база 2025=100, склад із Болгарією). Дані під старим
    # flowRef="ICP" заморожені на грудні 2025 і більше не оновлюються.
    # Код інституції-джерела теж змінився: "4" (Eurostat) → "4D0"
    # (Statistical Office of the European Commission). Джерело:
    # https://data.ecb.europa.eu/data/datasets/HICP/HICP.M.U2.N.000000.4D0.ANR
    "eurozone_hicp": ("HICP", "M.U2.N.000000.4D0.ANR"),
    # Deposit Facility Rate — ключова ставка ЄЦБ, щоденна, рівень
    "eurozone_deposit_rate": ("FM", "D.U2.EUR.4F.KR.DFR.LEV"),
    # Рівень безробіття, євроarea (changing composition), SA, %, 15-74 років
    "eurozone_unemployment_rate": ("LFSI", "M.U2.S.UNEHRT.TOTAL0.15_74.T"),
}


def _parse_time_period(value: str) -> Optional[date]:
    """ECB віддає TIME_PERIOD у різних форматах залежно від частоти
    ряду: YYYY-MM-DD (щоденні), YYYY-MM (місячні). Річні/квартальні
    ряди тут поки не використовуються, але лишаємо можливість
    розширити при потребі."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)  # YYYY-MM-DD
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y-%m").date()  # YYYY-MM → 1-ше число місяця
    except ValueError:
        return None


class EcbAdapter(BaseAdapter):
    source = "ecb"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(
                f"Невідомий metric_id для ECB: {metric_id!r}. "
                f"Доступні: {sorted(METRICS)}"
            )
        self.metric_id = metric_id
        self.flow_ref, self.series_key = METRICS[metric_id]
        self.session = session or requests.Session()

    def fetch(
        self,
        limit: Optional[int] = None,
        observation_start: Optional[str] = None,
        observation_end: Optional[str] = None,
    ) -> Any:
        # format=csvdata простіший і стабільніший для парсингу, ніж
        # рідний SDMX-JSON (там значення й дати рознесені по окремих
        # індексованих масивах, що значно ускладнює normalize()).
        url = f"{ECB_DATA_URL}/{self.flow_ref}/{self.series_key}"
        params: dict[str, Any] = {"format": "csvdata"}
        if limit:
            params["lastNObservations"] = limit
        if observation_start:
            params["startPeriod"] = observation_start
        if observation_end:
            params["endPeriod"] = observation_end

        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.text

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        records: list[NormalizedRecord] = []

        reader = csv.DictReader(io.StringIO(raw_response))
        for row in reader:
            raw_value = row.get("OBS_VALUE")
            if raw_value in (None, ""):
                logger.debug(
                    "Пропускаємо відсутнє значення %s за %s",
                    self.series_key, row.get("TIME_PERIOD"),
                )
                continue

            try:
                value = Decimal(raw_value)
            except InvalidOperation:
                logger.warning(
                    "Не вдалось розпарсити значення ECB %s за %s: %r",
                    self.series_key, row.get("TIME_PERIOD"), raw_value,
                )
                continue

            observed_at = _parse_time_period(row.get("TIME_PERIOD", ""))
            if observed_at is None:
                logger.warning("Пропущено запис без коректної дати: %r", row)
                continue

            records.append(
                NormalizedRecord(
                    source=self.source,
                    metric_id=self.metric_id,
                    value=value,
                    observed_at=observed_at,
                    fetched_at=fetched_at,
                    revision=None,  # визначається шаром збереження, див. common/db.py
                    raw_payload=dict(row),
                )
            )

        return records
