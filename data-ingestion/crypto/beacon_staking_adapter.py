"""
Обсяг ETH у стейкінгу — сума балансів АКТИВНИХ валідаторів, прямо з
консенсусного шару Ethereum (вузол Lighthouse на publicnode.com), без
ключа.

**Навіщо (2026-10-20).** "Обсяг стейкінгу" стояв в `ASSET_FACTOR_GAPS`
для ETH. Відтік зі стейкінгу — потенційна пропозиція на ринку, приплив
— ETH, що виходить з обігу.

**Чому так, а не beaconcha.in.** beaconcha.in вимагає ключ (живо
2026-10-20: "a valid API key is required"). Стандартний Beacon API не
має "загального балансу" — лише перелік УСІХ валідаторів (~1 млн
записів, сотні МБ). Lighthouse має власний ендпоінт
`/lighthouse/validator_inclusion/{epoch}/global` із
`current_epoch_active_gwei` — одне число. Живо: 43 760 823 ETH.

**Одне джерело — і це названо** (`config.FACTOR_SERIES`): це дані
самого блокчейну, а другий публічний вузол Lighthouse без ключа не
знайдено (Lodestar цього ендпоінта не має).

Береться ПОПЕРЕДНЯ епоха від голови ланцюга: стан найсвіжішої ще може
бути недоступний, а надто старої — вже обрізаний вузлом (живо: 404
"beacon state at slot ... not found").
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

BEACON_URL = "https://ethereum-beacon-api.publicnode.com"
_SLOTS_PER_EPOCH = 32
_GWEI_PER_ETH = Decimal("1000000000")

METRICS: dict[str, str] = {
    "eth_staked": "current_epoch_active_gwei",
}


class BeaconStakingAdapter(BaseAdapter):
    source = "beacon_lighthouse"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(f"Немає метрики консенсусного шару для {metric_id!r}. Доступні: {sorted(METRICS)}")
        self.metric_id = metric_id
        self.field = METRICS[metric_id]
        self.session = session or requests.Session()

    def fetch(self, **_ignored) -> Any:
        head = self.session.get(f"{BEACON_URL}/eth/v1/beacon/headers/head", timeout=30)
        head.raise_for_status()
        slot = int(head.json()["data"]["header"]["message"]["slot"])
        epoch = slot // _SLOTS_PER_EPOCH - 1
        response = self.session.get(f"{BEACON_URL}/lighthouse/validator_inclusion/{epoch}/global", timeout=30)
        response.raise_for_status()
        return {"epoch": epoch, "body": response.json()}

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        try:
            gwei = Decimal(str(raw_response["body"]["data"][self.field]))
        except (KeyError, TypeError) as error:
            raise ValueError(f"Beacon: у відповіді немає {self.field!r}: {str(raw_response)[:300]}") from error
        return [NormalizedRecord(
            source=self.source,
            metric_id=self.metric_id,
            value=(gwei / _GWEI_PER_ETH).quantize(Decimal("1")),
            observed_at=fetched_at.date(),
            fetched_at=fetched_at,
            raw_payload={"epoch": raw_response.get("epoch")},
        )]
