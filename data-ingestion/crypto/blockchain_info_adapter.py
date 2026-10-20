"""
blockchain.info Charts API — кількість підтверджених транзакцій
Bitcoin за добу, без ключа. ДРУГЕ незалежне джерело для звірки
`btc_tx_count` з CoinMetrics (`config.FACTOR_SERIES`).

**Живий замір 2026-10-20** на 7 спільних днях (08-14.10): розкид із
CoinMetrics 0.30-1.77% — різні індексатори трохи по-різному рахують
службові транзакції; той самий порядок і той самий рух день у день.

**Лаг ~6 днів**: о 20.10 остання точка — за 14.10. Тому як ОСНОВНЕ
джерело не годиться, а для звірки спільних дат — повністю.

Час точки — Unix-мітка початку доби UTC.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

API_URL = "https://api.blockchain.info/charts/{chart}"

METRICS: dict[str, str] = {
    "btc_tx_count": "n-transactions",
}


class BlockchainInfoAdapter(BaseAdapter):
    source = "blockchain_info"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(f"Немає графіка blockchain.info для {metric_id!r}. Доступні: {sorted(METRICS)}")
        self.metric_id = metric_id
        self.chart = METRICS[metric_id]
        self.session = session or requests.Session()

    def fetch(self, limit: Optional[int] = None, **_ignored) -> Any:
        days = limit or 30
        response = self.session.get(
            API_URL.format(chart=self.chart),
            params={"timespan": f"{days}days", "format": "json"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)

        if not isinstance(raw_response, dict) or raw_response.get("status") != "ok":
            raise ValueError(f"blockchain.info {self.chart}: неочікувана відповідь {raw_response!r}"[:500])

        records: list[NormalizedRecord] = []
        for point in raw_response.get("values") or []:
            try:
                observed_at = datetime.fromtimestamp(int(point["x"]), timezone.utc).date()
                value = Decimal(str(point["y"]))
            except (KeyError, TypeError, ValueError, InvalidOperation):
                logger.warning("blockchain.info %s: пропущено точку %r", self.chart, point)
                continue
            records.append(NormalizedRecord(
                source=self.source,
                metric_id=self.metric_id,
                value=value,
                observed_at=observed_at,
                fetched_at=fetched_at,
                raw_payload={"chart": self.chart},
            ))

        if not records:
            raise ValueError(f"blockchain.info {self.chart}: жодної точки у відповіді")
        return records
