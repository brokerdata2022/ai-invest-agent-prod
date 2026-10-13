"""
Адаптер OKX v5 public API (безстрокові SWAP-контракти проти USDT) —
третє з трьох джерел агрегації для крипто-скринінгу лонг/шорт/
спостереження (крок 2 з 5, docs/decisions.md 2026-09-27; той самий
інтерфейс, що crypto/binance_futures_adapter.py та
crypto/bybit_futures_adapter.py).
https://www.okx.com/docs-v5/en/

**На відміну від двох попередніх бірж, тут дешевий bulk-знімок НЕ дає
funding rate** — `/public/funding-rate` на OKX вимагає instId (немає
bulk-варіанта для всього ринку одразу, підтверджено документацією).
Тому `fetch_market_snapshot()` тут повертає лише volume + price change
(bulk `/market/tickers`) і Open Interest (bulk `/public/open-interest`,
OKX ЄДИНА з трьох бірж, де OI справді bulk) — funding rate йде окремою
`fetch_funding_rate()` per-instId, той самий принцип, що
`fetch_open_interest()` у Binance-адаптері: викликати лише для symbols,
що вже пройшли дешевший Tier A-фільтр, не для всього ринку одразу.

**Одиниці (важливо для майбутньої агрегації, крок 4):**
- `quote_volume` і `open_interest_usd` — обидва в $ (USDT). Живо
  виявлено 2026-09-27: сирі OKX-поля (`volCcy24h`/`oiCcy`) насправді в
  БАЗОВІЙ валюті (BTC для BTC-USDT-SWAP), не в USDT, попри назву
  "Ccy" — перевірено: сире `volCcy24h` ~38710 для BTC-USDT-SWAP,
  абсурдно мало для $-обсягу найліквіднішого контракту, але після
  множення на `last` (~$85000) дає ~$3.3 млрд — узгоджується з
  Binance/Bybit того ж моменту. Обидва множаться на `last` тут, в
  адаптері — щоб той самий $-стиль виходив назовні для ВСІХ трьох
  бірж, а не лишав приховану різницю одиниць на етап агрегації.
  Виняток — Binance: там per-symbol OI (`fetch_open_interest()`)
  лишається в базових одиницях (конвертація на виклику, бо там ціна
  доступна лише в окремому bulk-знімку, не тут).
- `price_change_percent` — OKX не віддає його напряму (на відміну від
  двох інших бірж) — рахуємо самі з `last`/`open24h`.
- Ключ symbol тут — рідний OKX instId (напр. "BTC-USDT-SWAP"), не
  "BTCUSDT" — зведення до спільного ключа для агрегації по 3 біржах —
  окремий крок (4), не тут.

Live-запит цього API живо підтверджено 2026-09-27 (форма відповіді й
обидва фікси одиниць — реальні дані користувача, не лише документація).
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

OKX_BASE_URL = "https://www.okx.com"
INSTRUMENTS_URL = f"{OKX_BASE_URL}/api/v5/public/instruments"
TICKERS_URL = f"{OKX_BASE_URL}/api/v5/market/tickers"
OPEN_INTEREST_URL = f"{OKX_BASE_URL}/api/v5/public/open-interest"
FUNDING_RATE_URL = f"{OKX_BASE_URL}/api/v5/public/funding-rate"

INST_TYPE = "SWAP"
QUOTE_SUFFIX = "-USDT-SWAP"


def list_perpetual_symbols(session: requests.Session) -> list[dict]:
    """Активні USDT-маржинальні SWAP-контракти з instruments. Повертає
    [{"symbol": "BTC-USDT-SWAP", "onboard_date": date(...) | None}, ...]."""
    response = session.get(INSTRUMENTS_URL, params={"instType": INST_TYPE}, timeout=30)
    response.raise_for_status()
    payload = response.json()

    symbols = []
    for entry in payload.get("data", []):
        inst_id = entry.get("instId", "")
        if entry.get("state") == "live" and inst_id.endswith(QUOTE_SUFFIX):
            list_time = entry.get("listTime")
            onboard_date = None
            if list_time:
                try:
                    onboard_date = datetime.fromtimestamp(int(list_time) / 1000, tz=timezone.utc).date()
                except (ValueError, TypeError):
                    pass
            symbols.append({"symbol": inst_id, "onboard_date": onboard_date})
    return symbols


def fetch_market_snapshot(session: requests.Session) -> dict[str, dict]:
    """Bulk-знімок ринку — 2 HTTP-запити (tickers + open-interest, обидва
    `instType=SWAP`, без per-symbol виклику). funding_rate НЕ включено
    (немає bulk-ендпоінта на OKX) — окремо, `fetch_funding_rate()`.
    Повертає {instId: {quote_volume, price_change_percent, open_interest_usd}} —
    обидва $-поля вже сконвертовані з базової валюти (див. docstring модуля)."""
    ticker_response = session.get(TICKERS_URL, params={"instType": INST_TYPE}, timeout=30)
    ticker_response.raise_for_status()
    ticker_payload = ticker_response.json()

    oi_response = session.get(OPEN_INTEREST_URL, params={"instType": INST_TYPE}, timeout=30)
    oi_response.raise_for_status()
    oi_payload = oi_response.json()

    oi_by_symbol: dict[str, Decimal] = {}
    for entry in oi_payload.get("data", []):
        inst_id = entry.get("instId")
        if not inst_id:
            continue
        try:
            oi_by_symbol[inst_id] = Decimal(entry["oiCcy"])
        except (KeyError, InvalidOperation, TypeError):
            logger.warning("OKX open-interest: пропущено %r — неповні дані", entry)

    snapshot: dict[str, dict] = {}
    for entry in ticker_payload.get("data", []):
        inst_id = entry.get("instId")
        if not inst_id or inst_id not in oi_by_symbol:
            continue
        try:
            last = Decimal(entry["last"])
            open_24h = Decimal(entry["open24h"])
            price_change_percent = ((last - open_24h) / open_24h * 100) if open_24h else Decimal(0)
            # volCcy24h/oiCcy — базова валюта, не USDT (докладніше
            # docstring модуля) — множимо на `last`, щоб звести обидва
            # до того самого $-стилю, що Binance/Bybit-адаптери.
            snapshot[inst_id] = {
                "quote_volume": Decimal(entry["volCcy24h"]) * last,
                "price_change_percent": price_change_percent,
                "open_interest_usd": oi_by_symbol[inst_id] * last,
            }
        except (KeyError, InvalidOperation, TypeError):
            logger.warning("OKX tickers: пропущено %r — неповні дані", entry)

    return snapshot


def fetch_funding_rate(session: requests.Session, inst_id: str) -> Optional[Decimal]:
    """Per-instId — немає bulk-ендпоінта для funding rate на OKX.
    Викликати ЛИШЕ для symbols, що вже пройшли дешевший Tier A-фільтр
    (fetch_market_snapshot), не для всього ринку одразу."""
    response = session.get(FUNDING_RATE_URL, params={"instId": inst_id}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or []
    if not data:
        logger.warning("OKX funding-rate: порожня відповідь для %s: %r", inst_id, payload)
        return None
    try:
        return Decimal(data[0]["fundingRate"])
    except (KeyError, InvalidOperation, TypeError, IndexError):
        logger.warning("OKX funding-rate: неповна відповідь для %s: %r", inst_id, payload)
        return None


class OkxFuturesAdapter(BaseAdapter):
    """Bulk-адаптер для raw_observations (volume + OI, обидва $ — той
    самий принцип rule 6, що інші два адаптери цього кроку).
    funding_rate НЕ входить сюди (per-symbol, окрема функція) — тому,
    на відміну від Binance/Bybit-адаптерів, тут лише 2 metric_id на
    symbol, не 2-3."""

    source = "okx_futures"

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()

    def fetch(self, **kwargs) -> Any:
        return fetch_market_snapshot(self.session)

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        observed_at = fetched_at.date()
        records: list[NormalizedRecord] = []

        for inst_id, data in raw_response.items():
            # "BTC-USDT-SWAP" -> "btc-usdt-swap" (metric_id-безпечний,
            # той самий принцип, що lower() у Binance/Bybit-адаптерах).
            metric_prefix = inst_id.lower()
            for suffix, value in (
                ("quote_volume", data["quote_volume"]),
                ("open_interest_usd", data["open_interest_usd"]),
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
