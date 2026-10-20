"""
Farside Investors — щоденні чисті потоки в спотові BTC- і ETH-ETF США
($ млн, сума по всіх фондах), скрапінг HTML-таблиці, без ключа.

**Навіщо (2026-10-20).** "Потоки в спотові ETF" стояли в
`ASSET_FACTOR_GAPS` для BTC: з 2024 року це один із головних каналів
попиту на BTC, а аналіз його не бачив.

**Одне джерело — і це названо (`config.FACTOR_SERIES`).** Зведені
щоденні потоки по ВСІХ фондах безкоштовно публікує лише Farside;
інші агрегатори (SoSoValue, CoinGlass) — за ключем/платно. В аналіз
ряд іде з позначкою "одне джерело".

**Формат (живо 2026-10-20):** найбільша `<table>` сторінки; рядок =
дата `19 Oct 2026` + по стовпцю на фонд + `Total`. Відтік — у дужках:
`(65.4)` = −65.4. `-` — фонд ще не існував АБО ще не звітував за
день. Внизу — підсумкові рядки (Total/Average/Maximum/Minimum), не
дати.

**Неповний день.** Farside заповнює рядок поступово, у міру звітів
фондів. Якщо в останньому рядку з'явився `-` там, де напередодні було
число, — день ще не закритий, і рядок пропускається: інакше в аналіз
пішла б часткова сума як остаточна. Наступний прогін його добере.
"""

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

METRICS: dict[str, str] = {
    "btc_etf_flow_usd_m": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
    "eth_etf_flow_usd_m": "https://farside.co.uk/ethereum-etf-flow-all-data/",
}

_TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.S)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"^\d{1,2} [A-Z][a-z]{2} \d{4}$")


def _cells(row_html: str) -> list[str]:
    return [re.sub(r"\s+", " ", _TAG_RE.sub("", c)).strip() for c in _CELL_RE.findall(row_html)]


def parse_flow(text: str) -> Optional[Decimal]:
    """`(65.4)` → −65.4, `1,234.5` → 1234.5, `-`/порожньо → None."""
    text = text.strip().replace(",", "")
    if text in ("", "-"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if negative else value


class FarsideAdapter(BaseAdapter):
    source = "farside"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(f"Немає сторінки Farside для {metric_id!r}. Доступні: {sorted(METRICS)}")
        self.metric_id = metric_id
        self.url = METRICS[metric_id]
        self.session = session or requests.Session()
        self._limit = 30

    def fetch(self, limit: Optional[int] = None, **_ignored) -> Any:
        self._limit = limit or 30
        response = self.session.get(self.url, headers=_HEADERS, timeout=40)
        response.raise_for_status()
        return response.text

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)

        tables = _TABLE_RE.findall(raw_response) if isinstance(raw_response, str) else []
        if not tables:
            raise ValueError(f"Farside {self.metric_id}: на сторінці немає таблиці — бот-захист або зміна формату?")
        table = max(tables, key=len)

        dated_rows: list[list[str]] = []
        for row_html in _ROW_RE.findall(table):
            cells = _cells(row_html)
            if cells and _DATE_RE.match(cells[0]):
                dated_rows.append(cells)

        if not dated_rows:
            raise ValueError(f"Farside {self.metric_id}: жодного рядка з датою — формат змінився?")

        # Перевірка на незакритий день — лише для ОСТАННЬОГО рядка: старші
        # дні вже остаточні, а фонд, що закрився, дав би "-" після чисел
        # і назавжди вибив би той день.
        last_incomplete = len(dated_rows) >= 2 and any(
            parse_flow(prev) is not None and parse_flow(cur) is None
            for prev, cur in zip(dated_rows[-2][1:-1], dated_rows[-1][1:-1])
        )

        records: list[NormalizedRecord] = []
        window = dated_rows[-self._limit:]
        for index, cells in enumerate(window):
            total = parse_flow(cells[-1])
            incomplete = last_incomplete and index == len(window) - 1
            if total is None or incomplete:
                logger.info(
                    "Farside %s: %s пропущено — %s", self.metric_id, cells[0],
                    "день ще не закритий (є фонди без звіту)" if incomplete else "немає суми",
                )
                continue
            records.append(NormalizedRecord(
                source=self.source,
                metric_id=self.metric_id,
                value=total,
                observed_at=datetime.strptime(cells[0], "%d %b %Y").date(),
                fetched_at=fetched_at,
                raw_payload={"row": cells},
            ))

        if not records:
            raise ValueError(f"Farside {self.metric_id}: жодного повного дня в таблиці")
        return records
