"""
Мінфін Японії — офіційні дані валютних ІНТЕРВЕНЦІЙ (Foreign Exchange
Intervention Operations), CSV із квітня 1991 року, без ключа.
Першоджерело: інтервенції проводить сам Мінфін (через Банк Японії).

**Навіщо (2026-10-20).** "Інтервенції Мінфіну Японії" стояли в
`ASSET_FACTOR_GAPS` для USD/JPY: саме вони різко розвертали пару
(2022, 2024, 2026), і без них аналіз не знав, коли інтервенція була
востаннє й якого розміру.

**Формат (живо 2026-10-20):** кодування CP932; одиниця — 100 млн ієн;
денний рядок: `令和8年,4月,30日,2026,Apr,30,"62,787",…,the US dollar
(sold) the Japanese yen (bought)`. Рік у наступних рядках того самого
кварталу ПОРОЖНІЙ (`,5月,4日,,May,4,…`) — переноситься з попереднього.
Квартальні підсумки (`April - June 2026`) — не дні, пропускаються.

**Знак:** + купівля ієни (продаж доларів; тисне USD/JPY вниз),
− продаж ієни. Ряд ПОДІЄВИЙ: точки лише в дні інтервенцій, тож
"давно не було нових точок" — норма, а не застарілість
(`config.FACTOR_SERIES`: свіжість не перевіряється).

**Лаг публікації:** денні дані Мінфін викладає щокварталу, тож
інтервенція поточного кварталу з'являється тут із запізненням до ~3
місяців; у реальному часі її покривають новини.
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

CSV_URL = (
    "https://www.mof.go.jp/english/policy/international_policy/reference/feio/"
    "foreign_exchange_intervention_operations.csv"
)

_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

METRICS: dict[str, str] = {
    "japan_fx_intervention": "100 million yen",
}

_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}


def parse_interventions(text: str) -> list[tuple[date, Decimal, str]]:
    """[(дата, сума зі знаком, опис)] у порядку файлу."""
    if "100 million Yen" not in text:
        raise ValueError("Мінфін Японії: у файлі немає позначки одиниць '100 million Yen' — формат змінився?")

    result = []
    year: Optional[int] = None
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 9:
            continue
        year_cell, month_cell, day_cell, amount_cell, pair = row[3].strip(), row[4].strip(), row[5].strip(), row[6], row[8]
        if year_cell.isdigit() and len(year_cell) == 4:
            year = int(year_cell)
        if month_cell not in _MONTHS or not day_cell.isdigit() or year is None:
            continue
        try:
            amount = Decimal(amount_cell.replace(",", ""))
        except InvalidOperation:
            continue
        pair_lower = pair.lower()
        if "yen (bought)" in pair_lower:
            signed = amount
        elif "yen (sold)" in pair_lower:
            signed = -amount
        else:
            # Інтервенції не проти ієни (напр. EUR/USD у 2000-х) на
            # USD/JPY прямо не тиснуть — у ряд не йдуть.
            continue
        result.append((date(year, _MONTHS[month_cell], int(day_cell)), signed, pair.strip()))
    return result


class MofJapanAdapter(BaseAdapter):
    source = "mof_japan"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(f"Немає ряду Мінфіну Японії для {metric_id!r}. Доступні: {sorted(METRICS)}")
        self.metric_id = metric_id
        self.session = session or requests.Session()
        self._limit = 30

    def fetch(self, limit: Optional[int] = None, **_ignored) -> Any:
        self._limit = limit or 30
        response = self.session.get(CSV_URL, headers=_HEADERS, timeout=40)
        response.raise_for_status()
        return response.content.decode("cp932", errors="replace")

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        interventions = parse_interventions(raw_response)
        if not interventions:
            raise ValueError("Мінфін Японії: жодного рядка інтервенції у файлі")

        interventions.sort(key=lambda item: item[0])
        return [
            NormalizedRecord(
                source=self.source,
                metric_id=self.metric_id,
                value=amount,
                observed_at=day,
                fetched_at=fetched_at,
                raw_payload={"pair": pair, "unit": "100 million yen"},
            )
            for day, amount, pair in interventions[-self._limit:]
        ]
