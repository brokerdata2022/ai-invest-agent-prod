"""
iShares Gold Trust (IAU) — кількість золота у фонді, тонн. Першоджерело:
сторінка емітента (BlackRock), число вбудоване в JSON сторінки.

**Навіщо (2026-10-20).** Після GLD (`spdr_gld_adapter.py`) прогалиною
лишались "потоки в інші золоті ETF". IAU — другий за розміром золотий
ETF США; разом із GLD це більша частина американських золотих ETF.

**Формат (живо 2026-10-20):** JSON в атрибутах HTML, екранований
(`&quot;`); після розекранування —
`"Tonnes in Trust","formattedValue":"462.93",...,"formattedAsOfDate":"Oct 19, 2026"`.
Поруч — "Ounces in Trust" 14 883 534.15, і 14 883 534.15 / 32 150.7466 =
462.93: внутрішня узгодженість перевіряється (незалежного другого
джерела тоннажу немає — це власна книга емітента).

Історії сторінка не дає — ряд накопичується щоденним збором.
"""

import html
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

PAGE_URL = "https://www.ishares.com/us/products/239561/ishares-gold-trust-fund"

_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

METRICS: dict[str, str] = {
    "iau_tonnes": "Tonnes in Trust",
}

_OUNCES_PER_TONNE = Decimal("32150.7466")
# Допуск внутрішньої перевірки тонни ↔ унції: обидва числа округлені
# на сторінці (тонни — до 0.01).
_CONSISTENCY_TONNES = Decimal("0.05")


def _field(page: str, label: str) -> tuple[Decimal, str]:
    match = re.search(
        rf'"{re.escape(label)}","formattedValue":"([\d,.]+)".{{0,400}}?"formattedAsOfDate":"([A-Za-z]{{3}} \d{{1,2}}, \d{{4}})"',
        page, re.S,
    )
    if not match:
        raise ValueError(f"iShares IAU: на сторінці немає поля {label!r} — формат змінився?")
    return Decimal(match.group(1).replace(",", "")), match.group(2)


class IsharesIauAdapter(BaseAdapter):
    source = "ishares_iau"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(f"Немає поля iShares IAU для {metric_id!r}. Доступні: {sorted(METRICS)}")
        self.metric_id = metric_id
        self.session = session or requests.Session()

    def fetch(self, **_ignored) -> Any:
        response = self.session.get(PAGE_URL, headers=_HEADERS, timeout=40)
        response.raise_for_status()
        return response.text

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        if not isinstance(raw_response, str):
            raise ValueError("iShares IAU: відповідь не текст сторінки")

        # JSON сидить в атрибутах HTML і екранований (&quot;) — без
        # розекранування поле не знаходиться (живо 2026-10-20: перший
        # замір розекрановував, адаптер — ні, і впав на тій самій сторінці).
        page = html.unescape(raw_response)
        tonnes, as_of = _field(page, "Tonnes in Trust")
        ounces, _ = _field(page, "Ounces in Trust")
        if abs(ounces / _OUNCES_PER_TONNE - tonnes) > _CONSISTENCY_TONNES:
            raise ValueError(
                f"iShares IAU: тонни {tonnes} не сходяться з унціями {ounces} "
                f"({ounces / _OUNCES_PER_TONNE:.2f} т) — сторінка віддала суперечливі числа"
            )

        return [NormalizedRecord(
            source=self.source,
            metric_id=self.metric_id,
            value=tonnes,
            observed_at=datetime.strptime(as_of, "%b %d, %Y").date(),
            fetched_at=fetched_at,
            raw_payload={"ounces": str(ounces), "as_of": as_of},
        )]
