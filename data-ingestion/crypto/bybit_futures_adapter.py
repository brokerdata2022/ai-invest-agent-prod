"""
Адаптер Bybit v5 public API (лінійні безстрокові USDT-контракти) —
другий з трьох джерел агрегації для крипто-скринінгу лонг/шорт/
спостереження (крок 2 з 5, docs/decisions.md 2026-09-27; крок 1 —
crypto/binance_futures_adapter.py, той самий інтерфейс).
https://bybit-exchange.github.io/docs/v5/market/tickers

На відміну від Binance (2 bulk-запити для volume+funding, окремий
per-symbol запит для OI): Bybit віддає volume, funding rate І Open
Interest В ОДНОМУ bulk-ендпоінті (`/v5/market/tickers`) — тут
`fetch_market_snapshot()` уже містить `open_interest_value`, окремої
`fetch_open_interest()` не потрібно.

**Одиниці, важливо для агрегації (крок майбутній, ще не написаний):**
- `price_change_percent` тут нормалізовано до ТОГО САМОГО стилю, що
  Binance (одиниці "відсоток", напр. 3.5 = 3.5%) — сирий Bybit-формат
  (`price24hPcnt`) це частка (0.035), тому `* 100` у normalize().
- `funding_rate` — частка (0.0001 = 0.01%), той самий стиль, що
  Binance `lastFundingRate` — конвертації не потрібно.
- `open_interest_value` — Bybit віддає одразу в $ (USDT), Binance — ні
  (лише кількість контрактів, `fetch_open_interest()` там повертає
  base-asset одиниці) — агрегація має явно множити Binance OI на
  mark_price, щоб звести до тих самих $-одиниць.

Live-запит цього API живо цією сесією НЕ підтверджено (той самий
чесний підхід, що crypto/binance_futures_adapter.py) — форма відповіді
взята з офіційної документації Bybit v5.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

BYBIT_BASE_URL = "https://api.bybit.com"
INSTRUMENTS_INFO_URL = f"{BYBIT_BASE_URL}/v5/market/instruments-info"
TICKERS_URL = f"{BYBIT_BASE_URL}/v5/market/tickers"

CATEGORY = "linear"
QUOTE_COIN = "USDT"
CONTRACT_TYPE = "LinearPerpetual"


def list_perpetual_symbols(session: requests.Session) -> list[dict]:
    """Активні лінійні безстрокові USDT-контракти з instruments-info.
    Повертає [{"symbol": "BTCUSDT", "onboard_date": date(...) | None}, ...]."""
    response = session.get(INSTRUMENTS_INFO_URL, params={"category": CATEGORY}, timeout=30)
    response.raise_for_status()
    payload = response.json()

    symbols = []
    for entry in payload.get("result", {}).get("list", []):
        if (
            entry.get("status") == "Trading"
            and entry.get("contractType") == CONTRACT_TYPE
            and entry.get("quoteCoin") == QUOTE_COIN
        ):
            launch_ms = entry.get("launchTime")
            onboard_date = None
            if launch_ms:
                try:
                    onboard_date = datetime.fromtimestamp(int(launch_ms) / 1000, tz=timezone.utc).date()
                except (ValueError, TypeError):
                    pass
            symbols.append({"symbol": entry["symbol"], "onboard_date": onboard_date})
    return symbols


def fetch_market_snapshot(session: requests.Session) -> dict[str, dict]:
    """Bulk-знімок усього ринку — ОДИН HTTP-запит (`/v5/market/tickers`
    містить volume, funding rate, Open Interest одночасно — на відміну
    від Binance, де це 2+1 окремих запити). Повертає {symbol:
    {quote_volume, price_change_percent, funding_rate, open_interest_value}}.

    Живо виявлено 2026-09-27: ця сама відповідь містить не лише
    безстрокові контракти, а й ДАТОВАНІ ф'ючерси (напр.
    "BTCUSDT-02OCT26") з порожнім `fundingRate` — `category=linear` на
    Bybit охоплює обидва типи, `/v5/market/instruments-info` (де є
    contractType) тут не запитується вдруге заради економії запиту.
    Замість спроби розпарсити порожній рядок і зловити помилку —
    відфільтровуємо датовані контракти НАПЕРЕД за конвенцією іменування
    Bybit (символ безстрокового контракту ніколи не містить "-";
    датовані завжди мають суфікс "-DDMMMYY")."""
    response = session.get(TICKERS_URL, params={"category": CATEGORY}, timeout=30)
    response.raise_for_status()
    payload = response.json()

    snapshot: dict[str, dict] = {}
    for entry in payload.get("result", {}).get("list", []):
        symbol = entry.get("symbol")
        if not symbol or "-" in symbol:
            continue
        try:
            snapshot[symbol] = {
                "quote_volume": Decimal(entry["turnover24h"]),
                # Bybit віддає частку (0.035) — приводимо до "відсотків"
                # (3.5), той самий стиль, що Binance priceChangePercent.
                "price_change_percent": Decimal(entry["price24hPcnt"]) * 100,
                "funding_rate": Decimal(entry["fundingRate"]),
                "open_interest_value": Decimal(entry["openInterestValue"]),
            }
        except (KeyError, InvalidOperation, TypeError):
            logger.warning("Bybit tickers: пропущено %r — неповні дані", entry)

    return snapshot


class BybitFuturesAdapter(BaseAdapter):
    """Bulk-адаптер для raw_observations (той самий принцип, що
    BinanceFuturesAdapter — rule 6, потрібна історія для самої
    скринінг-логіки, не лише поточне значення)."""

    source = "bybit_futures"

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()

    def fetch(self, **kwargs) -> Any:
        return fetch_market_snapshot(self.session)

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        observed_at = fetched_at.date()
        records: list[NormalizedRecord] = []

        for symbol, data in raw_response.items():
            metric_prefix = symbol.lower()
            for suffix, value in (
                ("quote_volume", data["quote_volume"]),
                ("funding_rate", data["funding_rate"]),
                ("open_interest_value", data["open_interest_value"]),
            ):
                records.append(
                    NormalizedRecord(
                        source=self.source,
                        metric_id=f"{metric_prefix}_{suffix}",
                        value=value,
                        observed_at=observed_at,
                        fetched_at=fetched_at,
                        revision=None,
                        raw_payload=data,
                    )
                )

        return records
