#!/usr/bin/env python3
"""
Діагностика ОДНОГО символу через увесь конвеєр крипто-скринінгу
(run_screening.py), з ПОВНИМИ причинами (reasons), які сам
run_screening.py відкидає (рахує лише загальний Tier A pass count, не
per-symbol reasons — docs/decisions.md). Для живих випадків "я бачу
памп на Х, чому його немає в SHORT/WATCH?" (той самий повторюваний
запит користувача — QNTUSDT/SOONUSDT, MAGMAUSDT, SANDUSDT — тепер
інструмент, а не ручний разовий розбір щоразу).

Не записує нічого в БД (крім читання history для OI/volume spike) —
безпечно запускати будь-коли, скільки завгодно разів.

Використання:
    python diagnose_symbol.py AINUSDT
    python diagnose_symbol.py AIN   # USDT додається сам, якщо відсутній
"""

import logging
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

import requests
from dotenv import load_dotenv

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Корінь репозиторію — для ЄДИНОГО `config.py` (рішення
# користувача 2026-10-04: один конфіг на весь агент).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))
sys.path.insert(0, _ANALYSIS_DIR)

from common.db import get_connection  # noqa: E402

from crypto.binance_futures_adapter import (  # noqa: E402
    list_perpetual_symbols as list_binance_symbols,
    fetch_market_snapshot as fetch_binance_snapshot,
    fetch_open_interest as fetch_binance_oi,
)
from crypto.bybit_futures_adapter import (  # noqa: E402
    list_perpetual_symbols as list_bybit_symbols,
    fetch_market_snapshot as fetch_bybit_snapshot,
)
from crypto.okx_futures_adapter import (  # noqa: E402
    list_perpetual_symbols as list_okx_symbols,
    fetch_market_snapshot as fetch_okx_snapshot,
)

from crypto_screening.aggregate_sources import aggregate_snapshots, canonicalize_symbol  # noqa: E402
from crypto_screening.tier_a import check_tier_a  # noqa: E402
from crypto_screening.long_screen import screen_long  # noqa: E402
from crypto_screening.short_watch_screen import screen_short_or_watch  # noqa: E402
from config import OI_WINDOW_DAYS_LONG, OI_WINDOW_DAYS_SHORT, VOLUME_LOOKBACK_DAYS  # noqa: E402

from run_screening import (  # noqa: E402
    compute_oi_change_pct,
    compute_price_indicators,
    compute_volume_spike_pct,
    pump_pct_from_aggregated,
)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


def _normalize(symbol: str) -> str:
    symbol = symbol.strip().upper()
    return symbol if symbol.endswith("USDT") else symbol + "USDT"


def main() -> None:
    load_dotenv()
    if len(sys.argv) != 2:
        sys.exit("Використання: python diagnose_symbol.py <SYMBOL>, напр. AINUSDT")
    symbol = _normalize(sys.argv[1])

    session = requests.Session()
    print(f"=== {symbol}: збір живих даних з Binance/Bybit/OKX ===")

    binance_symbols = list_binance_symbols(session)
    bybit_symbols = list_bybit_symbols(session)
    okx_symbols = list_okx_symbols(session)
    binance_snapshot = fetch_binance_snapshot(session)
    bybit_snapshot = fetch_bybit_snapshot(session)
    okx_snapshot = fetch_okx_snapshot(session)

    aggregated = aggregate_snapshots(binance_snapshot, bybit_snapshot, okx_snapshot)

    if symbol not in aggregated:
        print(f"❌ {symbol} НЕ знайдено на жодній з 3 бірж (bulk-знімок зараз) — немає що діагностувати.")
        return

    data = aggregated[symbol]

    oldest_onboard = None
    for exchange, symbols in (
        ("binance_futures", binance_symbols),
        ("bybit_futures", bybit_symbols),
        ("okx_futures", okx_symbols),
    ):
        for entry in symbols:
            if canonicalize_symbol(exchange, entry["symbol"]) != symbol or entry["onboard_date"] is None:
                continue
            if oldest_onboard is None or entry["onboard_date"] < oldest_onboard:
                oldest_onboard = entry["onboard_date"]

    today = datetime.now(timezone.utc).date()

    print(f"\nБіржі: {sorted(data['exchanges'])}")
    print(f"Обсяг $24г (сума бірж): ${data['quote_volume_24h']:,.0f}")
    print(f"Open Interest $ (без Binance, якщо нижче не збагачено): ${data['open_interest_usd']:,.0f}")
    print(f"Funding rate (середній): {data['funding_rate_avg']}")
    print(f"Дата лістингу (найстаріша серед бірж): {oldest_onboard}")

    tier_a = check_tier_a(symbol, data, oldest_onboard, today)
    print(f"\n=== Tier A: {'✅ ПРОЙШОВ' if tier_a.eligible else '❌ НЕ ПРОЙШОВ'} ===")
    for reason in tier_a.reasons:
        print(f"  - {reason}")

    binance_data = data["per_exchange"].get("binance_futures")
    if not tier_a.eligible and binance_data is not None:
        try:
            oi_base = fetch_binance_oi(session, symbol)
            if oi_base is not None:
                oi_usd = oi_base * binance_data["mark_price"]
                boosted = dict(data, open_interest_usd=data["open_interest_usd"] + oi_usd)
                tier_a_with_binance_oi = check_tier_a(symbol, boosted, oldest_onboard, today)
                print(
                    f"\n(З урахуванням Binance OI (+${oi_usd:,.0f}, не в bulk-знімку): "
                    f"{'✅ пройшов би' if tier_a_with_binance_oi.eligible else '❌ усе одно ні'})"
                )
                if tier_a_with_binance_oi.eligible:
                    tier_a = tier_a_with_binance_oi
                    data = boosted
        except Exception as exc:  # noqa: BLE001 — лише діагностика, не критично
            print(f"  (не вдалось перевірити Binance OI: {exc})")

    if not tier_a.eligible:
        print("\n⛔ Далі (LONG/SHORT/WATCH) не рахується — Tier A не пройдено.")
        return

    print("\n=== Індикатори ===")
    price = compute_price_indicators(session, symbol)
    pump_pct = pump_pct_from_aggregated(data)
    print(f"Памп (24г, максимум серед бірж): {pump_pct}")
    print(f"RSI (денний): {price['rsi']}")
    print(f"Тренд підтверджений (backtest проти naive): {price['trend_confirmed']}")

    conn = get_connection()
    try:
        oi_change_long = compute_oi_change_pct(conn, symbol, OI_WINDOW_DAYS_LONG)
        oi_change_short = compute_oi_change_pct(conn, symbol, OI_WINDOW_DAYS_SHORT)
        volume_spike = compute_volume_spike_pct(conn, symbol)
    finally:
        conn.close()
    print(f"Зміна OI за {OI_WINDOW_DAYS_LONG}д (LONG-вікно): {oi_change_long} (None — немає ще історії Bybit)")
    print(f"Зміна OI за {OI_WINDOW_DAYS_SHORT}д (SHORT-вікно): {oi_change_short} (None — немає ще історії Bybit)")
    print(f"Сплеск обсягу (vs {VOLUME_LOOKBACK_DAYS}д середнє): {volume_spike} (None — немає ще історії Bybit)")

    long_signal = screen_long(
        symbol=symbol, trend_confirmed=price["trend_confirmed"], oi_change_pct=oi_change_long,
        rsi_value=price["rsi"], funding_rate=data["funding_rate_avg"],
    )
    print(f"\n=== LONG: {'✅ qualifies' if long_signal.qualifies else '❌ ні'} ===")
    for reason in long_signal.reasons:
        print(f"  - {reason}")

    short_watch = screen_short_or_watch(
        symbol=symbol, pump_pct=pump_pct, oi_change_pct_after_pump=oi_change_short,
        volume_spike_pct=volume_spike, rsi_value=price["rsi"], funding_rate=data["funding_rate_avg"],
    )
    print(f"\n=== SHORT/WATCH: категорія = {short_watch.category} ===")
    for reason in short_watch.reasons:
        print(f"  - {reason}")


if __name__ == "__main__":
    main()
