"""
USDA FAS PSD (Production, Supply and Distribution) — оцінки врожаю
кави за країнами, тисячі мішків по 60 кг. Відкритий архів без ключа
(`psd_coffee_csv.zip`); REST API того самого PSD вимагає окремий ключ
USDA — ключ EIA він не приймає (живо 2026-10-20: "Bad API Key").

**Навіщо (2026-10-20).** "Оцінки врожаю" стояли в `ASSET_FACTOR_GAPS`
для кави. Бразилія — арабіка (ф'ючерс KC), В'єтнам — робуста.

**Що це за число.** Оцінка USDA на МАРКЕТИНГОВИЙ рік (Бразилія:
липень-червень), яку USDA переглядає двічі на рік (червень, грудень).
`observed_at` = 1 січня маркетингового року; перегляд оцінки — нова
ревізія того самого `observed_at` (`common/db.py`), тож історія
переглядів не губиться. Дата публікації оцінки — у `raw_payload`.

**Одне джерело — і це названо** (`config.FACTOR_SERIES`): оцінка CONAB
(Бразилія) — ІНША методологія й систематично інше число; звіряти їх
означало б вічну хибну тривогу (урок CUUR/CUSR).
"""

import csv
import io
import logging
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://apps.fas.usda.gov/psdonline/downloads/psd_coffee_csv.zip"

_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

# metric_id → (країна в архіві, атрибут)
METRICS: dict[str, tuple[str, str]] = {
    "brazil_coffee_production": ("Brazil", "Production"),
    "vietnam_coffee_production": ("Vietnam", "Production"),
}

_EXPECTED_UNIT = "(1000 60 KG BAGS)"


class UsdaPsdAdapter(BaseAdapter):
    source = "usda_psd"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(f"Немає ряду USDA PSD для {metric_id!r}. Доступні: {sorted(METRICS)}")
        self.metric_id = metric_id
        self.country, self.attribute = METRICS[metric_id]
        self.session = session or requests.Session()
        self._limit = 6

    def fetch(self, limit: Optional[int] = None, **_ignored) -> Any:
        self._limit = limit or 6
        response = self.session.get(ARCHIVE_URL, headers=_HEADERS, timeout=60)
        response.raise_for_status()
        return response.content

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)

        try:
            archive = zipfile.ZipFile(io.BytesIO(raw_response))
            name = archive.namelist()[0]
            rows = csv.DictReader(io.TextIOWrapper(archive.open(name), encoding="utf-8-sig"))
            selected = [
                r for r in rows
                if r.get("Country_Name") == self.country and r.get("Attribute_Description") == self.attribute
            ]
        except (zipfile.BadZipFile, IndexError, KeyError) as error:
            raise ValueError(f"USDA PSD: архів не читається — {error}") from error

        records = []
        for row in selected:
            if row.get("Unit_Description") != _EXPECTED_UNIT:
                raise ValueError(f"USDA PSD {self.metric_id}: одиниці {row.get('Unit_Description')!r}")
            try:
                market_year = int(row["Market_Year"])
                value = Decimal(row["Value"])
                # "66300.0000" → 66300; .normalize() дав би "6.63E+4".
                if value == value.to_integral_value():
                    value = value.to_integral_value()
            except (KeyError, ValueError, InvalidOperation):
                continue
            records.append(NormalizedRecord(
                source=self.source,
                metric_id=self.metric_id,
                value=value,
                observed_at=date(market_year, 1, 1),
                fetched_at=fetched_at,
                raw_payload={
                    "market_year": market_year,
                    "published": f"{row.get('Calendar_Year')}-{row.get('Month')}",
                },
            ))

        if not records:
            raise ValueError(f"USDA PSD: немає рядів {self.country}/{self.attribute} в архіві")
        records.sort(key=lambda r: r.observed_at)
        return records[-self._limit:]
