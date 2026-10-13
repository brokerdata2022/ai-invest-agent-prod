"""
Адаптер Binance public API — щоденні OHLCV-свічки для крипто-активів
watchlist (BTC/ETH/SOL, docs/watchlist.md, закрито 2026-09-25).
https://binance-docs.github.io/apidocs/spot/en/#kline-candlestick-data

Першоджерело (сама біржа), не агрегатор — на відміну від CoinGecko
(crypto/coingecko_adapter.py), який дає market_cap (метрику, якої в
принципі немає в даних однієї біржі). Ключ не потрібен — ендпоінт
публічний, без реєстрації (перевірено живим запитом 2026-09-26,
`GET /api/v3/klines?symbol=BTCUSDT&interval=1d&limit=3` → 200 з мережі
користувача, geo-block на відміну від деяких інших регіонів не
спостерігається).

METRICS — фіксований словник для ОРИГІНАЛЬНОГО закритого watchlist
(BTC/ETH/SOL, docs/watchlist.md) — зворотна сумісність із
`orchestration/jobs.py:_crypto_prices()`, що й досі перебирає METRICS
напряму. 2026-10-03 (докладніше docs/decisions.md, живий кейс
BNB/watchlist_add): конструктор отримав необов'язковий `symbol` —
якщо заданий, ОБХОДИТЬ METRICS повністю (символ напряму, як у
`quotes/twelvedata_adapter.py`) — для крипто-активів, доданих ПІЗНІШЕ
через Telegram `/watchlist_add` (`orchestration/telegram_commands.py`),
не з оригінального закритого списку.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# internal metric_id (== news/queries.py:WATCHLIST_ASSET_IDS) → Binance
# symbol (завжди проти USDT — найглибша ліквідність, стабільна прив'язка
# до долара для порівнянності з рештою watchlist-цін).
METRICS: dict[str, str] = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
}


class BinanceAdapter(BaseAdapter):
    source = "binance"

    def __init__(self, metric_id: str, symbol: Optional[str] = None, session: Optional[requests.Session] = None):
        if symbol is not None:
            self.symbol = symbol
        elif metric_id in METRICS:
            self.symbol = METRICS[metric_id]
        else:
            raise ValueError(
                f"Невідомий metric_id для Binance: {metric_id!r}. "
                f"Доступні: {sorted(METRICS)} (або передайте symbol= явно)"
            )
        self.metric_id = metric_id
        self.session = session or requests.Session()

    def fetch(self, limit: Optional[int] = None) -> Any:
        params: dict[str, Any] = {
            "symbol": self.symbol,
            "interval": "1d",
        }
        if limit:
            params["limit"] = limit

        response = self.session.get(BINANCE_KLINES_URL, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        records: list[NormalizedRecord] = []

        if not isinstance(raw_response, list):
            # Помилки Binance (напр. невірний symbol) приходять з
            # HTTP-кодом помилки (raise_for_status() у fetch() це вже
            # ловить) — сюди потрапляємо тільки якщо формат відповіді
            # несподівано змінився.
            logger.warning(
                "Неочікувана відповідь Binance для %s (%s) — очікувався список свічок: %r",
                self.metric_id, self.symbol, raw_response,
            )
            return records

        for row in raw_response:
            try:
                open_time_ms, open_, high, low, close, volume = row[0], row[1], row[2], row[3], row[4], row[5]
            except (IndexError, TypeError):
                logger.warning(
                    "Binance %s: неочікувана структура свічки, рядок пропущено: %r",
                    self.symbol, row,
                )
                continue

            observed_at = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).date()

            try:
                close_value = Decimal(close)
                volume_value = Decimal(volume)
            except InvalidOperation:
                logger.warning(
                    "Binance %s: не вдалось розпарсити close/volume за %s: %r",
                    self.symbol, observed_at, row,
                )
                continue

            raw_payload = {
                "open_time": open_time_ms,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }

            records.append(
                NormalizedRecord(
                    source=self.source,
                    metric_id=f"{self.metric_id}_close",
                    value=close_value,
                    observed_at=observed_at,
                    fetched_at=fetched_at,
                    revision=None,  # визначається шаром збереження, common/db.py
                    raw_payload=raw_payload,
                )
            )
            records.append(
                NormalizedRecord(
                    source=self.source,
                    metric_id=f"{self.metric_id}_volume",
                    value=volume_value,
                    observed_at=observed_at,
                    fetched_at=fetched_at,
                    revision=None,
                    raw_payload=raw_payload,
                )
            )

        return records
