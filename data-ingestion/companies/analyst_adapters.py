"""
Консенсус аналітиків і free float акції — з ТРЬОХ незалежних джерел,
щоб їх можна було звірити (`common/consensus.py`): правда — згода
джерел, не офіційність одного (рішення користувача 2026-10-12).

**Живий замір ПЕРЕД кодом (2026-10-22, SKYD).** Шукав, поки не
знайшов три джерела, що віддають і ціль, і float без ключа:

| джерело | ціль | аналітиків | float | акцій в обігу |
|---|---|---|---|---|
| Nasdaq API (`/api/analyst/…/targetprice`) | $9.34 | 11 (2/5/4) | — | 1.093 млрд |
| Finviz (сторінка тикера) | $8.96 | — (рек. 3.28) | 536.7 млн | 1.09 млрд |
| stockanalysis.com (статистика) | $8.87 | 22 | 4.10 млрд | 5.04 млрд |
| MarketBeat (не підключено) | $5.59 | 10 | 1.117 млрд | — |
| Yahoo quoteSummary | 429 на crumb із цього IP — не підключено | | | |

Що це означає: **ціль** у трьох підключених джерел сходиться в межах
~5% (MarketBeat — викид); **float розходиться у вісім разів**. Причина
не в помилці парсингу: Finviz рахує float як акції в обігу мінус
частку інсайдерів (1.09 млрд × (1 − 52.3%) ≈ 0.52 млрд), stockanalysis
уже бачить емісію після угоди 06.10.2026 (8-K: завершення придбання,
продаж акцій без реєстрації) — 5.04 млрд акцій. Тобто одне число
"free float" без звірки й дати тут було б неправдою, і аналіз мусить
бачити розбіжність, а не вибране навмання значення.

Усі три — НЕОФІЦІЙНІ (data-ingestion/CLAUDE.md): структура може
змінитись без попередження, тому несподівана відповідь — `ValueError`
з назвою тикера, а не тихий нуль записів.

metric_id: `{ticker}_{поле}`, поля — `FIELDS` нижче. `observed_at` —
день знімка (консенсус — поточний стан, не історичний ряд).
"""

import logging
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Поля знімка — суфікси metric_id. Однакові між джерелами там, де
# значення однакове за змістом: саме за ними й іде звірка.
FIELDS = (
    "target_mean", "target_low", "target_high", "analyst_count",
    "rating_buy", "rating_hold", "rating_sell", "recommendation",
    "float_shares", "shares_outstanding", "insider_own_pct", "short_float_pct",
)

_MULTIPLIERS = {"K": Decimal("1e3"), "M": Decimal("1e6"), "B": Decimal("1e9"), "T": Decimal("1e12")}


def parse_number(text: Optional[str]) -> Optional[Decimal]:
    """'536.71M' → 536710000, '52.27%' → 52.27, '$8.87' → 8.87,
    '4,100,069,013' → 4100069013, '-' / '' → None."""
    if text is None:
        return None
    cleaned = text.strip().replace(",", "").replace("$", "").replace("%", "")
    if cleaned in ("", "-", "n/a", "N/A"):
        return None
    multiplier = Decimal(1)
    if cleaned[-1:].upper() in _MULTIPLIERS:
        multiplier = _MULTIPLIERS[cleaned[-1].upper()]
        cleaned = cleaned[:-1]
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    # Ціле число акцій, а не '5.3671E+8' (той самий урок, що USDA
    # "7.19E+4", 2026-10-20).
    return (value * multiplier).quantize(Decimal(1)) if multiplier != 1 else value


class _SnapshotAdapter(BaseAdapter):
    """Спільне: тикер → знімок полів на сьогодні."""

    def __init__(self, ticker: str, session: Optional[requests.Session] = None):
        if not ticker or not ticker.strip():
            raise ValueError(f"Некоректний тикер: {ticker!r}")
        self.ticker = ticker.strip().upper()
        self.session = session or requests.Session()

    def _records(self, values: dict[str, Optional[Decimal]], raw: dict) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        today = fetched_at.date()
        records = [
            NormalizedRecord(
                source=self.source,
                metric_id=f"{self.ticker.lower()}_{field}",
                value=value,
                observed_at=today,
                fetched_at=fetched_at,
                raw_payload=raw,
            )
            for field, value in values.items()
            if value is not None
        ]
        if not records:
            raise ValueError(f"{self.source} {self.ticker}: жодного поля не розпізнано")
        return records


class NasdaqAnalystAdapter(_SnapshotAdapter):
    """Nasdaq API: ціль і розподіл рекомендацій. Живо 2026-10-22:
    `{"data": {"consensusOverview": {"lowPriceTarget": 6.0,
    "highPriceTarget": 16.0, "priceTarget": 9.34, "buy": 2, "sell": 4,
    "hold": 5}}}`. Без браузерних заголовків — таймаут."""

    source = "nasdaq_analyst"
    URL = "https://api.nasdaq.com/api/analyst/{ticker}/targetprice"

    def fetch(self, **kwargs) -> Any:
        response = self.session.get(
            self.URL.format(ticker=self.ticker),
            headers={**_BROWSER_HEADERS, "Accept": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        try:
            overview = raw_response["data"]["consensusOverview"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Nasdaq analyst {self.ticker}: несподівана структура ({exc})") from exc
        if not isinstance(overview, dict):
            raise ValueError(f"Nasdaq analyst {self.ticker}: немає consensusOverview")

        def num(key):
            value = overview.get(key)
            return None if value is None else parse_number(str(value))

        buy, hold, sell = num("buy"), num("hold"), num("sell")
        counts = [c for c in (buy, hold, sell) if c is not None]
        return self._records({
            "target_mean": num("priceTarget"),
            "target_low": num("lowPriceTarget"),
            "target_high": num("highPriceTarget"),
            "rating_buy": buy,
            "rating_hold": hold,
            "rating_sell": sell,
            "analyst_count": sum(counts) if counts else None,
        }, raw={"consensusOverview": overview})


class FinvizAdapter(_SnapshotAdapter):
    """Finviz: таблиця знімка на сторінці тикера. Живо 2026-10-22:
    `<div class="snapshot-td-label">Shs Float</div></td><td …><div
    class="snapshot-td-content"><b>536.71M</b>` — значення в НАСТУПНІЙ
    комірці після підпису (часом підпис і значення обгорнуті посиланням).
    `recommendation` — шкала Finviz 1 (купувати) … 5 (продавати)."""

    source = "finviz"
    URL = "https://finviz.com/quote.ashx?t={ticker}&p=d"
    LABELS = {
        "Target Price": "target_mean",
        "Recom": "recommendation",
        "Shs Float": "float_shares",
        "Shs Outstand": "shares_outstanding",
        "Insider Own": "insider_own_pct",
        "Short Float": "short_float_pct",
    }

    def fetch(self, **kwargs) -> Any:
        response = self.session.get(
            self.URL.format(ticker=self.ticker),
            headers={**_BROWSER_HEADERS, "Accept": "text/html"},
            timeout=30,
        )
        response.raise_for_status()
        return response.text

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        values, raw = {}, {}
        for label, field in self.LABELS.items():
            match = re.search(
                rf">{re.escape(label)}(?:</a>)?</div></td>\s*<td[^>]*>(.*?)</td>",
                raw_response or "", re.S,
            )
            if match is None:
                continue
            text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
            raw[label] = text
            values[field] = parse_number(text)
        if not raw:
            raise ValueError(f"Finviz {self.ticker}: таблиця знімка не знайдена (змінилась розмітка?)")
        return self._records(values, raw=raw)


class StockAnalysisAdapter(_SnapshotAdapter):
    """stockanalysis.com, сторінка статистики. Дані вбудовані в
    скрипт сторінки: `{id:"float",title:"Float",value:"4.10B",
    hover:"4,100,069,013"}` — беремо `hover` (точне число), а не
    округлене `value`."""

    source = "stockanalysis"
    URL = "https://stockanalysis.com/stocks/{ticker}/statistics/"
    IDS = {
        "priceTarget": "target_mean",
        "analystCount": "analyst_count",
        "float": "float_shares",
        "sharesout": "shares_outstanding",
        "sharesInsiders": "insider_own_pct",
        "shortFloat": "short_float_pct",
    }

    def fetch(self, **kwargs) -> Any:
        response = self.session.get(
            self.URL.format(ticker=self.ticker.lower()),
            headers={**_BROWSER_HEADERS, "Accept": "text/html"},
            timeout=30,
        )
        response.raise_for_status()
        return response.text

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        values, raw = {}, {}
        for match in re.finditer(
            r'\{id:"(\w+)",title:"[^"]*",value:"([^"]*)",hover:"([^"]*)"', raw_response or "",
        ):
            key, shown, exact = match.groups()
            if key in self.IDS and self.IDS[key] not in values:
                raw[key] = exact or shown
                values[self.IDS[key]] = parse_number(exact) or parse_number(shown)
        if not raw:
            raise ValueError(f"stockanalysis {self.ticker}: вбудованих даних не знайдено (змінилась розмітка?)")
        return self._records(values, raw=raw)


ANALYST_ADAPTERS = (NasdaqAnalystAdapter, FinvizAdapter, StockAnalysisAdapter)


def today_utc() -> date:
    return datetime.now(timezone.utc).date()
