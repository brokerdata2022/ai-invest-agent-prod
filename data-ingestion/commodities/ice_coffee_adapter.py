"""
ICE Futures U.S. — щоденний звіт про СЕРТИФІКОВАНІ запаси кави "C"
(арабіка) на складах біржі, мішки по 60 кг. Першоджерело: біржа сама
веде облік того, що може бути поставлено за ф'ючерсом.

**Навіщо (2026-10-20).** Сертифіковані запаси — пряма міра доступної
до поставки арабіки; їхнє падіння тисне на ф'ючерс угору. Стояло в
`ASSET_FACTOR_GAPS` для кави.

**Формат.** Файл на кожен торговий день,
`coffee_cert_stock_YYYYMMDD.xls` — старий двійковий Excel (BIFF,
згенерований Crystal Reports), тому потрібен `xlrd` (у requirements).
У неторговий день — 404, тож адаптер іде назад по датах. Живо
2026-10-16: "TOTAL BAGS CERTIFIED" → рядок "Total in Bags" → 240 767.

**Одне джерело — і це названо** (`config.FACTOR_SERIES`): облік веде
лише біржа.
"""

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import requests
import xlrd

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

URL = "https://www.ice.com/publicdocs/futures_us_reports/coffee/coffee_cert_stock_{day:%Y%m%d}.xls"

_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

METRICS: dict[str, str] = {
    "coffee_certified_bags": "TOTAL BAGS CERTIFIED",
}

# Скільки календарних днів назад шукати файли: вихідні + свята біржі.
_MAX_BACKFILL_DAYS = 60
# Пауза між запитами. Живо 2026-10-20: добір історії (--limit 120) без
# паузи — ~40 запитів поспіль — і ICE відповів 429.
_REQUEST_PAUSE_SECONDS = 1.0
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"
_AS_OF_RE = re.compile(r"As of:\s*([A-Za-z]{3} \d{1,2}, \d{4})")


def parse_report(content: bytes, section: str) -> tuple[date, Decimal]:
    """(дата звіту, перший "Total in Bags" після заголовка секції)."""
    book = xlrd.open_workbook(file_contents=content)
    sheet = book.sheet_by_index(0)
    rows = [[str(sheet.cell_value(r, c)).strip() for c in range(sheet.ncols)] for r in range(sheet.nrows)]

    as_of = None
    for row in rows:
        for cell in row:
            match = _AS_OF_RE.search(cell)
            if match:
                as_of = datetime.strptime(match.group(1), "%b %d, %Y").date()
                break
        if as_of:
            break
    if as_of is None:
        raise ValueError("ICE: у звіті немає рядка 'As of:' — формат змінився?")

    in_section = False
    for row in rows:
        cells = [c for c in row if c]
        if not cells:
            continue
        if cells[0] == section:
            in_section = True
            continue
        if in_section and cells[0] == "Total in Bags":
            return as_of, Decimal(cells[-1]).quantize(Decimal("1"))
    raise ValueError(f"ICE: у звіті немає секції {section!r} з підсумком — формат змінився?")


class IceCoffeeAdapter(BaseAdapter):
    source = "ice"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(f"Немає звіту ICE для {metric_id!r}. Доступні: {sorted(METRICS)}")
        self.metric_id = metric_id
        self.section = METRICS[metric_id]
        self.session = session or requests.Session()

    def fetch(self, limit: Optional[int] = None, today: Optional[date] = None, **_ignored) -> Any:
        """Звіти за останні `limit` торгових днів (дефолт 5), ідучи назад
        від сьогодні. 404 — неторговий день, не помилка."""
        wanted = limit or 5
        day = today or date.today()
        files: list[bytes] = []
        for attempt in range(min(_MAX_BACKFILL_DAYS, wanted * 2 + 7)):
            if attempt:
                time.sleep(_REQUEST_PAUSE_SECONDS)
            response = self.session.get(URL.format(day=day), headers=_HEADERS, timeout=30)
            if response.status_code == 200 and response.content.startswith(_OLE_MAGIC):
                files.append(response.content)
                if len(files) >= wanted:
                    break
            elif response.status_code == 429 and files:
                # Ліміт посеред добору ІСТОРІЇ: свіжі звіти вже є — їх
                # зберігаємо, а не викидаємо через старіші. Без повтору:
                # повтор ліміт лише поглиблює (правило проєкту).
                logger.warning(
                    "ICE: 429 на %s — зупиняю добір історії, зібрано %d звітів із %d",
                    day, len(files), wanted,
                )
                break
            elif response.status_code != 404:
                response.raise_for_status()
            day -= timedelta(days=1)
        return files

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        if not raw_response:
            raise ValueError("ICE: жодного звіту за останні дні — адреса змінилась чи біржа не публікує?")

        records = []
        for content in raw_response:
            as_of, bags = parse_report(content, self.section)
            records.append(NormalizedRecord(
                source=self.source,
                metric_id=self.metric_id,
                value=bags,
                observed_at=as_of,
                fetched_at=fetched_at,
                raw_payload={"section": self.section},
            ))
        return sorted(records, key=lambda r: r.observed_at)
