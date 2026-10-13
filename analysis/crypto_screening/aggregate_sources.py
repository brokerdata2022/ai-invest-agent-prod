"""
Зведення bulk-знімків трьох бірж (Binance/Bybit/OKX Futures) в один
per-symbol агрегований вигляд — крок 4 з 5 (docs/decisions.md
2026-09-27). Чиста функція (rule 1, CLAUDE.md) — приймає вже готові
snapshot-дикти (результат `fetch_market_snapshot()` кожного адаптера,
data-ingestion/crypto/*_futures_adapter.py), не робить мережевих
викликів сама.

**Канонічний ключ symbol** — стиль Binance/Bybit ("BTCUSDT"); OKX
("BTC-USDT-SWAP") зводиться до нього через `canonicalize_symbol()`.

**Одиниці** — усі $-однорідні (кожен адаптер уже нормалізує це сам,
docs/decisions.md, "живі баги" 2026-09-27), КРІМ Binance Open Interest:
на відміну від Bybit (`open_interest_value`, bulk, $) і OKX
(`open_interest_usd`, bulk, $, після фіксу), Binance не має bulk OI
взагалі — `fetch_open_interest()` там per-symbol, викликається пізніше
ЛИШЕ для Tier A-виживших (crypto/binance_futures_adapter.py). Тому
`aggregate_snapshots()` тут дає ЧАСТКОВИЙ `open_interest_usd` (сума
лише з Bybit/OKX, де вони присутні) — `enrich_with_binance_oi()` додає
Binance-частину ПІСЛЯ того, як Tier A вже відфільтрував кандидатів
(той самий принцип "дешевий bulk спершу, дороге per-symbol — лише для
виживших", що вже в самому Binance-адаптері).

**Агрегація по метриках:**
- `quote_volume_24h`/`open_interest_usd` — СУМА по біржах (більше
  ліквідності на кількох майданчиках одночасно, не середнє).
- `funding_rate_avg`/`price_change_percent_avg` — СЕРЕДНЄ (це ставки/
  відсотки, не адитивні величини).
- `exchange_count` — на скількох із 3 бірж символ узагалі торгується
  зараз — безкоштовний proxy "легітимності" (Tier A, крок 4
  продовження), не судження про якість проєкту.
"""

from decimal import Decimal
from typing import Optional

OKX_SUFFIX = "-USDT-SWAP"


def canonicalize_symbol(exchange: str, raw_symbol: str) -> str:
    """"BTC-USDT-SWAP" (OKX) -> "BTCUSDT". Binance/Bybit symbols уже
    канонічні (обидві біржі використовують той самий "BTCUSDT"-стиль)."""
    if exchange == "okx_futures" and raw_symbol.endswith(OKX_SUFFIX):
        return raw_symbol[: -len(OKX_SUFFIX)] + "USDT"
    return raw_symbol


def aggregate_snapshots(
    binance_snapshot: dict[str, dict],
    bybit_snapshot: dict[str, dict],
    okx_snapshot: dict[str, dict],
) -> dict[str, dict]:
    """Повертає {canonical_symbol: {quote_volume_24h, open_interest_usd
    (частковий, без Binance — див. docstring модуля), funding_rate_avg,
    price_change_percent_avg, exchange_count, exchanges, per_exchange}}."""
    merged: dict[str, dict] = {}

    for exchange, snapshot in (
        ("binance_futures", binance_snapshot),
        ("bybit_futures", bybit_snapshot),
        ("okx_futures", okx_snapshot),
    ):
        for raw_symbol, data in snapshot.items():
            symbol = canonicalize_symbol(exchange, raw_symbol)
            entry = merged.setdefault(
                symbol,
                {
                    "quote_volume_24h": Decimal(0),
                    "open_interest_usd": Decimal(0),
                    "funding_rates": [],
                    "price_change_percents": [],
                    "exchanges": set(),
                    "per_exchange": {},
                },
            )
            entry["quote_volume_24h"] += data["quote_volume"]
            entry["exchanges"].add(exchange)
            entry["per_exchange"][exchange] = data

            if "funding_rate" in data:
                entry["funding_rates"].append(data["funding_rate"])
            if "price_change_percent" in data:
                entry["price_change_percents"].append(data["price_change_percent"])
            # Bybit і OKX називають $-OI по-різному (див. docstring
            # адаптерів) — Binance взагалі не бере участі тут (немає в
            # bulk-знімку, докладніше docstring модуля).
            if "open_interest_value" in data:
                entry["open_interest_usd"] += data["open_interest_value"]
            elif "open_interest_usd" in data:
                entry["open_interest_usd"] += data["open_interest_usd"]

    result: dict[str, dict] = {}
    for symbol, entry in merged.items():
        funding_rates = entry["funding_rates"]
        price_changes = entry["price_change_percents"]
        result[symbol] = {
            "quote_volume_24h": entry["quote_volume_24h"],
            "open_interest_usd": entry["open_interest_usd"],
            "funding_rate_avg": (sum(funding_rates) / len(funding_rates)) if funding_rates else None,
            "price_change_percent_avg": (sum(price_changes) / len(price_changes)) if price_changes else None,
            "exchange_count": len(entry["exchanges"]),
            "exchanges": sorted(entry["exchanges"]),
            "per_exchange": entry["per_exchange"],
        }
    return result


def enrich_with_binance_oi(aggregated: dict[str, dict], binance_oi_usd: dict[str, Decimal]) -> None:
    """Додає Binance Open Interest (уже сконвертований у $ викликом,
    `fetch_open_interest() * mark_price` — сирий Binance OI в базових
    одиницях, докладніше crypto/binance_futures_adapter.py) для
    символів, що вже пройшли Tier A. Мутує `aggregated` на місці —
    викликається ПІСЛЯ Tier A, тому НЕ на весь ринок, лише на виживших."""
    for symbol, oi_usd in binance_oi_usd.items():
        if symbol in aggregated and oi_usd is not None:
            aggregated[symbol]["open_interest_usd"] += oi_usd
