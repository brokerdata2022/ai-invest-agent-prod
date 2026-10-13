#!/usr/bin/env python3
"""
Одноразовий backfill історичного Open Interest + обсягу (quote_volume)
для Bybit USDT-перпетуалів.

Жива діра (знайдена користувачем 2026-10-02): LONG/SHORT-скринінг
(analysis/crypto_screening/run_screening.py) рахує зміну OI/обсягу за
кілька днів (OI_WINDOW_DAYS_LONG=7 → 8 точок, VOLUME_LOOKBACK_DAYS=7 →
8 точок) з raw_observations (source='bybit_futures') — а щоденна
джоба crypto_derivatives_collect (з 2026-09-27) пише лише ОДНУ точку
на день, тобто природним шляхом довелось би чекати ще кілька днів,
перш ніж LONG/SHORT узагалі зможе щось знайти, навіть якщо ринок уже
дає сигнал (живий приклад того самого дня: MAGMAUSDT +41.35% на
Binance — SHORT/WATCH критерій не впав через памп, а через відсутню
історію OI/обсягу для розрахунку дельти).

Bybit має готові історичні ендпоінти (на відміну від bulk-знімка
tickers, що дає лише "зараз", який уже використовує
bybit_futures_adapter.py:fetch_market_snapshot()) — обидва живо
підтверджено запитом перед написанням коду (той самий принцип, що
docs/decisions.md 2026-08-29):
- `/v5/market/kline` (category=linear, interval=D) — денні свічки:
  close price + turnover (= quote_volume У USDT НАПРЯМУ, без
  конвертації — на відміну від OI нижче).
- `/v5/market/open-interest` (category=linear, intervalTime=1d) —
  openInterest У БАЗОВІЙ валюті (контрактах), ПОТРЕБУЄ множення на
  close price ТОГО Ж ДНЯ, щоб звести до $-одиниць (open_interest_value)
  — той самий принцип юніт-нормалізації, що вже застосований ПРЯМО В
  АДАПТЕРІ для OKX (`volCcy24h`/`oiCcy` * `last`, docs/decisions.md
  2026-09-27) — тут аналогічно, у шарі data-ingestion, не в analysis/
  (critical rule 1 CLAUDE.md: нормалізація одиниць — не "аналіз").
  Обидва ендпоінти повертають timestamp'и на ТІЙ САМІЙ денній сітці
  (UTC-північ), тож зведення по ts напряму, без округлення днів.

Пише в raw_observations ТИМИ САМИМИ metric_id, що щоденна джоба
(`{symbol}_open_interest_value`/`{symbol}_quote_volume`,
source='bybit_futures') — append-only, ЛИШЕ для днів, де даних ще
НЕМА взагалі (fetch_covered_dates()). **Навмисно НЕ "ревізує" дні, які
вже зібрала щоденна джоба** (з 2026-09-27) — свіжа денна свічка-close
й живий знімок на момент запуску джоби — це ДВІ різні методології
вимірювання того самого дня, не "виправлення" одна одної; створення
revision для вже покритого дня лише підмінило б точніший живий знімок
менш точною оцінкою в `v_observations_latest_revision` (яку й читають
compute_oi_change_pct/compute_volume_spike_pct) — живий баг, знайдений
користувачем 2026-10-02 одразу на --limit 5 тесті, виправлено тим же
днем. Ідемпотентно: повторний запуск нічого не додає, якщо прогалин
уже немає.

Використання:
    python backfill_bybit_derivatives.py                # усі USDT-перпетуали, 10 днів
    python backfill_bybit_derivatives.py --days 10
    python backfill_bybit_derivatives.py --limit 5        # тест на підмножині символів
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.adapter import NormalizedRecord  # noqa: E402
from common.db import get_connection, insert_observations  # noqa: E402

from crypto.bybit_futures_adapter import BYBIT_BASE_URL, CATEGORY, list_perpetual_symbols  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

KLINE_URL = f"{BYBIT_BASE_URL}/v5/market/kline"
OPEN_INTEREST_URL = f"{BYBIT_BASE_URL}/v5/market/open-interest"
SOURCE = "bybit_futures"

# Публічні market-data ендпоінти Bybit без автентифікації — 150мс між
# запитами (~6-7/с) залишає щедрий запас під задокументований ліміт,
# не підтверджений живо до верхньої межі (не критично: при 429
# requests.raise_for_status() кине виняток, except Exception нижче
# пропустить символ, не впустить увесь прогін).
REQUEST_PAUSE_SECONDS = 0.15


def fetch_daily_klines(session: requests.Session, symbol: str, days: int) -> dict[int, dict[str, Decimal]]:
    """{timestamp_ms: {"close", "turnover"}} — turnover уже в USDT
    (Bybit kline, 7-й елемент кожної свічки), жодної конвертації не
    потрібно (на відміну від Open Interest нижче)."""
    response = session.get(
        KLINE_URL, params={"category": CATEGORY, "symbol": symbol, "interval": "D", "limit": days}, timeout=15
    )
    response.raise_for_status()
    payload = response.json()

    out: dict[int, dict[str, Decimal]] = {}
    for entry in payload.get("result", {}).get("list", []):
        try:
            ts_ms, _open, _high, _low, close, _volume, turnover = entry
            out[int(ts_ms)] = {"close": Decimal(close), "turnover": Decimal(turnover)}
        except (ValueError, InvalidOperation, TypeError):
            logger.warning("%s: не вдалось розпарсити kline-рядок %r", symbol, entry)
    return out


def fetch_daily_open_interest(session: requests.Session, symbol: str, days: int) -> dict[int, Decimal]:
    """{timestamp_ms: openInterest} — БАЗОВА валюта (контракти), caller
    множить на close price ТОГО Ж ts зі fetch_daily_klines()."""
    response = session.get(
        OPEN_INTEREST_URL,
        params={"category": CATEGORY, "symbol": symbol, "intervalTime": "1d", "limit": days},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()

    out: dict[int, Decimal] = {}
    for entry in payload.get("result", {}).get("list", []):
        try:
            out[int(entry["timestamp"])] = Decimal(entry["openInterest"])
        except (KeyError, InvalidOperation, TypeError):
            logger.warning("%s: не вдалось розпарсити open-interest рядок %r", symbol, entry)
    return out


def build_records(
    symbol: str,
    klines: dict[int, dict[str, Decimal]],
    open_interest: dict[int, Decimal],
    fetched_at: datetime,
    covered_volume_dates: frozenset = frozenset(),
    covered_oi_dates: frozenset = frozenset(),
) -> list[NormalizedRecord]:
    """quote_volume — для КОЖНОГО дня з klines (turnover уже $), КРІМ
    днів, що вже є в covered_volume_dates. open_interest_value —
    аналогічно, крім covered_oi_dates, і лише днів, де є ОБИДВІ точки
    (ts спільний для klines і open_interest) — Bybit зберігає історію
    OI коротшим вікном, ніж kline, для щойно-лістингованих контрактів
    ts-перетин може бути частковим, це очікувано, не помилка.

    Чому `covered_*_dates`, а не просто "перезаписати" (нова ревізія):
    щоденна джоба (crypto_derivatives_collect) вже пише ЖИВИЙ знімок
    (bulk tickers API) для днів з 2026-09-27 — денна свічка-close тут
    інша МЕТОДОЛОГІЯ вимірювання (значення на момент закриття доби, не
    на момент запуску джоби), не "більш правильна" ревізія того самого
    факту. Створення revision для таких днів лише підмінює точніший
    живий знімок менш точною оцінкою в `v_observations_latest_revision`
    (яку й читає compute_oi_change_pct/compute_volume_spike_pct) —
    живий баг, знайдений користувачем 2026-10-02 одразу на --limit 5
    тесті. Цей backfill має сенс ЛИШЕ для днів, де даних ще НЕМА
    взагалі (до старту щоденної джоби) — caller передає вже наявні
    дати одним SELECT на символ (fetch_covered_dates())."""
    records = []
    symbol_lower = symbol.lower()
    for ts, kline in klines.items():
        observed_at = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        if observed_at not in covered_volume_dates:
            records.append(
                NormalizedRecord(
                    source=SOURCE, metric_id=f"{symbol_lower}_quote_volume",
                    value=kline["turnover"], observed_at=observed_at, fetched_at=fetched_at,
                )
            )
        if observed_at in covered_oi_dates:
            continue
        oi_base = open_interest.get(ts)
        if oi_base is not None:
            records.append(
                NormalizedRecord(
                    source=SOURCE, metric_id=f"{symbol_lower}_open_interest_value",
                    value=oi_base * kline["close"], observed_at=observed_at, fetched_at=fetched_at,
                )
            )
    return records


def fetch_covered_dates(conn, metric_id: str) -> frozenset:
    """Дати, для яких (source='bybit_futures', metric_id) уже має
    хоч один запис у raw_observations — незалежно від revision (навіть
    revision 1 означає "уже зібрано", backfill не повинен туди лізти)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT observed_at FROM raw_observations WHERE source = %s AND metric_id = %s",
            (SOURCE, metric_id),
        )
        return frozenset(row[0] for row in cur.fetchall())


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=10, help="Глибина історії в днях (за замовчуванням 10)")
    parser.add_argument("--limit", type=int, default=None, help="Лише перші N символів (тест)")
    args = parser.parse_args()

    session = requests.Session()
    symbols = [s["symbol"] for s in list_perpetual_symbols(session)]
    if args.limit:
        symbols = symbols[: args.limit]
    logger.info("Backfill історії (%d днів) для %d Bybit-символів", args.days, len(symbols))

    conn = get_connection()
    fetched_at = datetime.now(timezone.utc)
    total_inserted = 0
    failed: list[str] = []
    try:
        for i, symbol in enumerate(symbols, start=1):
            try:
                klines = fetch_daily_klines(session, symbol, args.days)
                time.sleep(REQUEST_PAUSE_SECONDS)
                open_interest = fetch_daily_open_interest(session, symbol, args.days)
                time.sleep(REQUEST_PAUSE_SECONDS)

                symbol_lower = symbol.lower()
                covered_volume = fetch_covered_dates(conn, f"{symbol_lower}_quote_volume")
                covered_oi = fetch_covered_dates(conn, f"{symbol_lower}_open_interest_value")

                records = build_records(
                    symbol, klines, open_interest, fetched_at,
                    covered_volume_dates=covered_volume, covered_oi_dates=covered_oi,
                )
                if records:
                    total_inserted += insert_observations(conn, records)
            except Exception:
                logger.error("%s: backfill провалився", symbol, exc_info=True)
                failed.append(symbol)

            if i % 50 == 0:
                logger.info("[%d/%d] опрацьовано...", i, len(symbols))
    finally:
        conn.close()

    logger.info("Готово: %d записів (нових/змінених)", total_inserted)
    if failed:
        logger.warning("Провалились %d символів: %s", len(failed), ", ".join(failed[:30]))


if __name__ == "__main__":
    main()
