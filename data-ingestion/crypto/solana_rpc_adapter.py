"""
Активність мережі Solana — темп НЕ-голосувальних транзакцій (тобто
користувацьких; голосування валідаторів — службовий консенсус), з
публічних RPC-вузлів самої мережі, без ключа.

**Навіщо (2026-10-20).** "Кількість транзакцій мережі Solana" стояла в
`ASSET_FACTOR_GAPS`: CoinMetrics безкоштовно мережевих метрик SOL не
дає, blockchair забанив IP після одного запиту.

**Як рахується.** `getRecentPerformanceSamples` — щохвилинні зразки
вузла (`numNonVoteTransactions`, `samplePeriodSecs`). Сума за останні
240 зразків (~4 год), перерахована на добу. Вікно ФІКСОВАНЕ й однакове
для обох вузлів: офіційний вузол тримає лише ~270 зразків (живо), а
різні вікна дали б різні числа на тих самих даних (12% — живо, 270 проти
720 зразків). Прогін щодня в той самий час — значення співставні день
у день.

**Звірка (`config.FACTOR_SERIES`):** два незалежні вузли —
publicnode.com і офіційний api.mainnet-beta.solana.com. Живо 2026-10-20
за однакове вікно: 135 081 222 проти 135 094 476 (0.01%). Ловить
вузол, що відстав чи віддає сміття.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

ENDPOINTS: dict[str, str] = {
    "solana_publicnode": "https://solana-rpc.publicnode.com",
    "solana_mainnet": "https://api.mainnet-beta.solana.com",
}

METRICS: dict[str, str] = {
    "sol_nonvote_tx_per_day": "numNonVoteTransactions",
}

SAMPLES = 240
_SECONDS_PER_DAY = 86400


class SolanaRpcAdapter(BaseAdapter):
    """Один клас на обидва вузли: `source` — який саме, щоб у БД це були
    два окремі джерела, придатні для звірки."""

    def __init__(self, metric_id: str, source: str = "solana_publicnode",
                 session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(f"Немає метрики Solana для {metric_id!r}. Доступні: {sorted(METRICS)}")
        if source not in ENDPOINTS:
            raise ValueError(f"Невідомий вузол Solana {source!r}. Доступні: {sorted(ENDPOINTS)}")
        self.metric_id = metric_id
        self.source = source
        self.field = METRICS[metric_id]
        self.session = session or requests.Session()

    def fetch(self, **_ignored) -> Any:
        response = self.session.post(
            ENDPOINTS[self.source],
            json={"jsonrpc": "2.0", "id": 1, "method": "getRecentPerformanceSamples", "params": [SAMPLES]},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        samples = (raw_response or {}).get("result") if isinstance(raw_response, dict) else None
        if not samples:
            raise ValueError(f"Solana {self.source}: немає зразків: {str(raw_response)[:300]}")
        if len(samples) < SAMPLES:
            raise ValueError(
                f"Solana {self.source}: лише {len(samples)} зразків замість {SAMPLES} — "
                f"вікно не співставне з іншим вузлом"
            )

        seconds = sum(s["samplePeriodSecs"] for s in samples)
        transactions = sum(s.get(self.field, 0) for s in samples)
        per_day = (Decimal(transactions) / Decimal(seconds) * _SECONDS_PER_DAY).quantize(Decimal("1"))
        return [NormalizedRecord(
            source=self.source,
            metric_id=self.metric_id,
            value=per_day,
            observed_at=fetched_at.date(),
            fetched_at=fetched_at,
            raw_payload={"samples": len(samples), "seconds": seconds, "from_slot": samples[-1].get("slot"),
                         "to_slot": samples[0].get("slot")},
        )]
