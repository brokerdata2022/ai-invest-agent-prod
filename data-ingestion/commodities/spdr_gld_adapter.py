"""
SPDR Gold Shares (GLD) — щоденна кількість золота у фонді, тонн.
Першоджерело: емітент публікує власний архів (xlsx) із 2004 року.

**Навіщо (2026-10-20).** "Потоки в золоті ETF" стояли в
`ASSET_FACTOR_GAPS` для золота. GLD — найбільший золотий ETF у світі;
зміна його тоннажу — це і є приплив/відтік інвесторів у фізичне
золото через біржу.

**Одне джерело — і це названо (`config.FACTOR_SERIES`).** Тоннаж фонду
— власна книга емітента; незалежного публікатора того самого числа
безкоштовно немає. В аналіз ряд іде з позначкою "одне джерело".

**Чому xlsx читається стандартною бібліотекою.** xlsx — це zip з XML.
Тягнути openpyxl у образ заради одного стовпця — нова залежність і
перезбірка образу на продакшені; zip+XML читаються і так.

**Формат (живо 2026-10-20):** аркуш "US GLD Historical Archive",
рядок заголовків із "Date" і "Tonnes of Gold"; дата — рядок
`19-Oct-2026`; у вихідні/свята в стовпцях чисел стоїть текст
("US Holiday") — такі рядки пропускаються.
"""

import io
import logging
import re
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from xml.etree import ElementTree

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://api.spdrgoldshares.com/api/v1/historical-archive?product=gld&exchange=NYSE&lang=en"
SHEET_NAME = "US GLD Historical Archive"

_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

METRICS: dict[str, str] = {
    "gld_tonnes": "Tonnes of Gold",
}

_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_COL_RE = re.compile(r"^([A-Z]+)")


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")) for si in root.findall("m:si", _NS)]


def _sheet_path(zf: zipfile.ZipFile, sheet_name: str) -> str:
    workbook = ElementTree.fromstring(zf.read("xl/workbook.xml"))
    rel_id = None
    for sheet in workbook.iter(f"{{{_NS['m']}}}sheet"):
        if sheet.get("name") == sheet_name:
            rel_id = sheet.get(f"{{{_NS['r']}}}id")
    if rel_id is None:
        raise ValueError(f"SPDR GLD: у файлі немає аркуша {sheet_name!r}")
    rels = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.findall("rel:Relationship", _NS):
        if rel.get("Id") == rel_id:
            target = rel.get("Target").lstrip("/")
            return target if target.startswith("xl/") else f"xl/{target}"
    raise ValueError(f"SPDR GLD: не знайдено файл аркуша {sheet_name!r}")


def read_sheet_rows(xlsx_bytes: bytes, sheet_name: str) -> list[dict[str, str]]:
    """Рядки аркуша як {літера стовпця: текст клітинки}."""
    zf = zipfile.ZipFile(io.BytesIO(xlsx_bytes))
    strings = _shared_strings(zf)
    root = ElementTree.fromstring(zf.read(_sheet_path(zf, sheet_name)))
    rows: list[dict[str, str]] = []
    for row in root.iter(f"{{{_NS['m']}}}row"):
        cells: dict[str, str] = {}
        for cell in row.findall("m:c", _NS):
            match = _COL_RE.match(cell.get("r", ""))
            value_el = cell.find("m:v", _NS)
            if match is None:
                continue
            if cell.get("t") == "s" and value_el is not None:
                text = strings[int(value_el.text)]
            elif cell.get("t") == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(f"{{{_NS['m']}}}t"))
            else:
                text = value_el.text if value_el is not None else ""
            cells[match.group(1)] = (text or "").strip()
        rows.append(cells)
    return rows


class SpdrGldAdapter(BaseAdapter):
    source = "spdr_gld"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(f"Немає стовпця SPDR GLD для {metric_id!r}. Доступні: {sorted(METRICS)}")
        self.metric_id = metric_id
        self.column_title = METRICS[metric_id]
        self.session = session or requests.Session()
        self._limit = 30

    def fetch(self, limit: Optional[int] = None, **_ignored) -> Any:
        self._limit = limit or 30
        response = self.session.get(ARCHIVE_URL, headers=_HEADERS, timeout=60)
        response.raise_for_status()
        return response.content

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)

        if not isinstance(raw_response, (bytes, bytearray)) or not raw_response.startswith(b"PK"):
            raise ValueError("SPDR GLD: відповідь не xlsx — адреса архіву змінилась чи бот-захист?")

        rows = read_sheet_rows(bytes(raw_response), SHEET_NAME)

        date_col = value_col = None
        header_index = None
        for index, cells in enumerate(rows):
            by_title = {text: col for col, text in cells.items()}
            if "Date" in by_title and self.column_title in by_title:
                date_col, value_col = by_title["Date"], by_title[self.column_title]
                header_index = index
                break
        if header_index is None:
            raise ValueError(f"SPDR GLD: немає заголовків 'Date' і {self.column_title!r} — формат змінився?")

        records: list[NormalizedRecord] = []
        for cells in rows[header_index + 1:]:
            raw_date, raw_value = cells.get(date_col, ""), cells.get(value_col, "")
            try:
                observed_at = datetime.strptime(raw_date, "%d-%b-%Y").date()
                # Excel зберігає число як double: "1054.20000000000005".
                # repr(float) — найкоротший точний запис ("1054.2"), без
                # довільного округлення. Без цього кожне перезбирання
                # могло б давати "ревізію" на шумі останнього знака.
                value = Decimal(repr(float(raw_value.replace(",", ""))))
            except (ValueError, InvalidOperation):
                continue
            records.append(NormalizedRecord(
                source=self.source,
                metric_id=self.metric_id,
                value=value,
                observed_at=observed_at,
                fetched_at=fetched_at,
                raw_payload={"column": self.column_title},
            ))

        if not records:
            raise ValueError("SPDR GLD: жодного рядка з датою й тоннажем")
        records.sort(key=lambda r: r.observed_at)
        return records[-self._limit:]
