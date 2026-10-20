"""
Etherscan — щоденна кількість транзакцій Ethereum із публічного CSV
графіка (`/chart/tx?output=csv`), без ключа. ДРУГЕ незалежне джерело
для звірки `eth_tx_count` з CoinMetrics (`config.FACTOR_SERIES`).

**Чому CSV графіка, а не API.** Ендпоінт API `stats/dailytx` —
лише для платного PRO-рівня. CSV графіка віддається без ключа й без
бот-захисту (живо 2026-10-20, звичайний GET).

**Живий замір 2026-10-20**: збіг із CoinMetrics день у день ТОЧНИЙ
(16-19.10: 1 770 145 / 1 563 246 / 1 595 980 / 1 748 461).

Формат: `"Date(UTC)","UnixTimeStamp","Value"`, дата як `M/D/YYYY`.
Файл — уся історія з 2015 року (~4 тис. рядків), тож береться хвіст.
"""

import csv
import io
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

CSV_URL = "https://etherscan.io/chart/{chart}?output=csv"

_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

METRICS: dict[str, str] = {
    "eth_tx_count": "tx",
}


class EtherscanAdapter(BaseAdapter):
    source = "etherscan"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(f"Немає графіка Etherscan для {metric_id!r}. Доступні: {sorted(METRICS)}")
        self.metric_id = metric_id
        self.chart = METRICS[metric_id]
        self.session = session or requests.Session()
        self._limit = 30

    def fetch(self, limit: Optional[int] = None, **_ignored) -> Any:
        self._limit = limit or 30
        response = self.session.get(CSV_URL.format(chart=self.chart), headers=_HEADERS, timeout=30)
        response.raise_for_status()
        return response.text

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)

        if not isinstance(raw_response, str) or "UnixTimeStamp" not in raw_response[:200]:
            # Замість CSV прийшла HTML-сторінка (бот-захист, зміна адреси) —
            # тихий порожній результат вимкнув би звірку непомітно.
            raise ValueError(f"Etherscan {self.chart}: відповідь не схожа на CSV графіка: {str(raw_response)[:200]!r}")

        rows = list(csv.DictReader(io.StringIO(raw_response.lstrip("﻿"))))
        records: list[NormalizedRecord] = []
        for row in rows[-self._limit:]:
            try:
                observed_at = datetime.fromtimestamp(int(row["UnixTimeStamp"]), timezone.utc).date()
                value = Decimal(row["Value"])
            except (KeyError, TypeError, ValueError, InvalidOperation):
                logger.warning("Etherscan %s: пропущено рядок %r", self.chart, row)
                continue
            records.append(NormalizedRecord(
                source=self.source,
                metric_id=self.metric_id,
                value=value,
                observed_at=observed_at,
                fetched_at=fetched_at,
                raw_payload={"chart": self.chart, "date_text": row.get("Date(UTC)")},
            ))

        if not records:
            raise ValueError(f"Etherscan {self.chart}: жодної точки в CSV")
        return records
