"""
DefiLlama — щоденний TVL (кошти в DeFi-протоколах) мережі, без ключа.

**Навіщо (2026-10-20).** Для SOL "он-чейн активність" — єдина
прогалина з `ASSET_FACTOR_GAPS`, і закрити її кількістю транзакцій не
вийшло: CoinMetrics безкоштовно не дає мережевих метрик SOL (живо
2026-10-20), blockchair забанив IP після одного запиту. TVL — інша, але
змістовна міра: скільки капіталу реально працює в мережі.

**Одне джерело — і це названо (`config.FACTOR_SERIES`).** Інші
агрегатори TVL або платні, або рахують за ІНШОЮ методологією; звірка
різних методологій давала б вічну хибну тривогу (урок CUUR/CUSR,
`macro/bls_adapter.py`).

**Поточна доба.** Остання точка в DefiLlama — СЬОГОДНІ, і вона
оновлюється протягом дня. Тут вона відкидається: в аналіз іде лише
закрита доба (інакше значення "за сьогодні" в БД змінювалось би
ревізіями щогодини).
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

API_URL = "https://api.llama.fi/v2/historicalChainTvl/{chain}"

METRICS: dict[str, str] = {
    "sol_chain_tvl": "Solana",
}


class DefiLlamaAdapter(BaseAdapter):
    source = "defillama"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(f"Немає мережі DefiLlama для {metric_id!r}. Доступні: {sorted(METRICS)}")
        self.metric_id = metric_id
        self.chain = METRICS[metric_id]
        self.session = session or requests.Session()
        self._limit = 30

    def fetch(self, limit: Optional[int] = None, **_ignored) -> Any:
        self._limit = limit or 30
        response = self.session.get(API_URL.format(chain=self.chain), timeout=30)
        response.raise_for_status()
        return response.json()

    def normalize(self, raw_response: Any, today: Optional[date] = None) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        today = today or fetched_at.date()

        if not isinstance(raw_response, list):
            raise ValueError(f"DefiLlama {self.chain}: неочікувана відповідь {str(raw_response)[:300]!r}")

        records: list[NormalizedRecord] = []
        for point in raw_response[-(self._limit + 1):]:
            try:
                observed_at = datetime.fromtimestamp(int(point["date"]), timezone.utc).date()
                value = Decimal(str(point["tvl"]))
            except (KeyError, TypeError, ValueError, InvalidOperation):
                logger.warning("DefiLlama %s: пропущено точку %r", self.chain, point)
                continue
            if observed_at >= today:
                continue
            records.append(NormalizedRecord(
                source=self.source,
                metric_id=self.metric_id,
                value=value.quantize(Decimal("1")),
                observed_at=observed_at,
                fetched_at=fetched_at,
                raw_payload={"chain": self.chain},
            ))

        if not records:
            raise ValueError(f"DefiLlama {self.chain}: жодної закритої доби у відповіді")
        return records[-self._limit:]
