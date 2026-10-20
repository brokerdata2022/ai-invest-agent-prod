"""
CoinMetrics Community API — он-чейн активність BTC/ETH (кількість
транзакцій за добу), без ключа.

**Навіщо (2026-10-20).** "Он-чейн активність" стояла в
`ASSET_FACTOR_GAPS` для BTC і ETH: аналіз крипти бачив ціну, обсяг
бірж і ставки, але не те, що відбувається в самій мережі.

**Чому CoinMetrics основне, а не blockchain.info/Etherscan.** Одне API
на обидві мережі з однаковою методологією, без ключа, і свіже: точка
за вчора доступна наступного дня. blockchain.info відстає на ~6 днів —
придатний для ЗВІРКИ спільних дат, але не як основний ряд.

**Межа безкоштовного рівня (живо 2026-10-20):** для SOL community-API
мережевих метрик не дає взагалі ("not available with supplied
credentials") — лише ціну/капіталізацію. Тому SOL тут немає.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

API_URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"

# metric_id → (актив CoinMetrics, метрика CoinMetrics)
METRICS: dict[str, tuple[str, str]] = {
    "btc_tx_count": ("btc", "TxCnt"),
    "eth_tx_count": ("eth", "TxCnt"),
}


class CoinMetricsAdapter(BaseAdapter):
    source = "coinmetrics"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(f"Немає метрики CoinMetrics для {metric_id!r}. Доступні: {sorted(METRICS)}")
        self.metric_id = metric_id
        self.asset, self.cm_metric = METRICS[metric_id]
        self.session = session or requests.Session()

    def fetch(self, limit: Optional[int] = None, **_ignored) -> Any:
        days = limit or 30
        params = {
            "assets": self.asset,
            "metrics": self.cm_metric,
            "frequency": "1d",
            "start_time": (date.today() - timedelta(days=days)).isoformat(),
            "page_size": str(min(days + 5, 10000)),
        }
        response = self.session.get(API_URL, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)

        if not isinstance(raw_response, dict) or "error" in raw_response:
            raise ValueError(f"CoinMetrics {self.asset}/{self.cm_metric}: помилка API: {raw_response!r}"[:500])

        records: list[NormalizedRecord] = []
        for row in raw_response.get("data") or []:
            raw_value = row.get(self.cm_metric)
            if raw_value is None:
                continue
            try:
                value = Decimal(str(raw_value))
                observed_at = date.fromisoformat(str(row["time"])[:10])
            except (KeyError, ValueError, InvalidOperation):
                logger.warning("CoinMetrics %s: пропущено нерозбірний рядок %r", self.metric_id, row)
                continue
            records.append(NormalizedRecord(
                source=self.source,
                metric_id=self.metric_id,
                value=value,
                observed_at=observed_at,
                fetched_at=fetched_at,
                raw_payload={"asset": self.asset, "metric": self.cm_metric},
            ))

        if not records:
            raise ValueError(f"CoinMetrics {self.asset}/{self.cm_metric}: жодної точки у відповіді")
        return records
