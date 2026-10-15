#!/usr/bin/env python3
"""
Оркестрація крипто-скринінгу лонг/шорт/спостереження — крок 4.5 з 5
(docs/archive/plan-journal-2026-10-11.md Фаза 4, докладніше docs/decisions.md 2026-09-27). Зводить усе
докупи: 3 біржі (fetch_market_snapshot) → агрегація
(aggregate_sources.py) → Tier A (tier_a.py) → для виживших: RSI/тренд
(Binance klines) + памп (`pump_pct_from_aggregated()` — максимум 24h-
руху ціни серед бірж, де символ є) + Binance OI (per-symbol, лише тут,
не для всього ринку) → LONG/SHORT/WATCH (long_screen.py/
short_watch_screen.py).

**Історія — кросбіржова з 2026-10-15** (docs/decisions.md): раніше
`oi_change_pct`/`volume_spike_pct` читались ЛИШЕ з Bybit, бо на старті
збору (27.09) історії не було ні в кого, і один самоузгоджений ряд був
дешевшим за вирівнювання трьох. Зміряно через 18 днів збору: з 1056
символів ринку історію мали 855 — тобто 201 символ отримував None
НАЗАВЖДИ, а `screen_long`/`screen_short_or_watch` трактують None як
"критерій не пройдено", тож такий символ не міг стати ні LONG, ні
SHORT (максимум WATCH). Дані Binance/OKX при цьому збирались щодня й
не читались жодним механізмом.
  Тепер `_history_db.py` читає всі три біржі (кожна зі своїм
  написанням символу й назвою метрики), а `history.py` зводить їх у
  денний ряд СУМОЮ — так само, як `aggregate_sources.py` робить із
  живим знімком. Беруться лише біржі, присутні на ОБОХ краях вікна:
  інакше новий лістинг посеред вікна дав би стрибок суми, який
  виглядав би як приріст OI. Символ з однією біржею поводиться точно
  як раніше.
- RSI/тренд — з ЦІНИ БІРЖІ (Binance klines) напряму — НЕ залежать від
  власного збору, доступні одразу, але потребують, щоб символ узагалі
  торгувався на Binance (не завжди так — символи з Bybit/OKX без
  Binance-лістингу отримають rsi=None/trend_confirmed=False).
- `pump_pct` — живо виправлено 2026-09-27, ДВІЧІ: (1) РАНІШЕ рахувався
  з 5-денного вікна тих самих Binance klines і мовчки пропускав реальні
  одноденні пампи (живий приклад від користувача: QUSDT +83%,
  QNTUSDT +52%, SOONUSDT +46%, той самий день, на Binance) — тепер
  береться з УЖЕ агрегованого 24h-руху ціни, доступного для КОЖНОГО
  Tier A-символу одразу, незалежно від klines-історії чи
  Binance-лістингу. (2) `pump_pct_from_aggregated()` бере МАКСИМУМ
  (не середнє) серед бірж, де символ присутній — середнє розмило б
  реальний памп, якщо він проявився сильно лише на одній біржі (той
  самий стиль читання сигналу, що й сам користувач: дивиться на
  конкретну біржу, не на середнє по трьох).

Використання:
    python run_screening.py
    python run_screening.py --limit 20   # лише перші N Tier A-виживших (тест)
"""

import argparse
import logging
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

import requests
from dotenv import load_dotenv

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Корінь репозиторію — для ЄДИНОГО `config.py` (рішення
# користувача 2026-10-04: один конфіг на весь агент).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))
sys.path.insert(0, _ANALYSIS_DIR)

from common.db import get_connection, insert_observations  # noqa: E402

from crypto.binance_futures_adapter import (  # noqa: E402
    list_perpetual_symbols as list_binance_symbols,
    fetch_market_snapshot as fetch_binance_snapshot,
    fetch_open_interest as fetch_binance_oi,
    fetch_klines,
    open_interest_records,
)
from crypto.bybit_futures_adapter import (  # noqa: E402
    list_perpetual_symbols as list_bybit_symbols,
    fetch_market_snapshot as fetch_bybit_snapshot,
)
from crypto.okx_futures_adapter import (  # noqa: E402
    list_perpetual_symbols as list_okx_symbols,
    fetch_market_snapshot as fetch_okx_snapshot,
)

from crypto_screening.aggregate_sources import (  # noqa: E402
    aggregate_snapshots,
    canonicalize_symbol,
    enrich_with_binance_oi,
)
from crypto_screening._history_db import (  # noqa: E402
    KIND_OI,
    KIND_VOLUME,
    fetch_history_rows,
)
from crypto_screening.history import (  # noqa: E402
    consistent_daily_series,
    oi_change_pct,
    volume_spike_pct,
)
from crypto_screening.tier_a import check_tier_a  # noqa: E402
from crypto_screening.indicators import rsi  # noqa: E402
from crypto_screening.long_screen import screen_long  # noqa: E402
from crypto_screening.short_watch_screen import screen_short_or_watch  # noqa: E402
from crypto_screening._candidates_db import upsert_candidate  # noqa: E402
from crypto_screening._long_db import save_long_run  # noqa: E402
from config import (  # noqa: E402
    KLINES_LIMIT,
    OI_WINDOW_DAYS_LONG,
    MONITORING_KLINE_INTERVAL,
    MONITORING_KLINES_LIMIT,
    OI_WINDOW_DAYS_SHORT,
    PUMP_THRESHOLD_PCT,
    RSI_PERIOD,
    VOLUME_LOOKBACK_DAYS,
)

from forecasting.backtest import backtest_metric  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Скільки днів історії діставати з БД під вікно розрахунку. Вікна —
# OI_WINDOW_DAYS_LONG/SHORT і VOLUME_LOOKBACK_DAYS (config.py); беремо
# із запасом, бо день без збору (збій біржі/мережі) не має обривати
# вікно — consistent_daily_series() сама візьме потрібні N днів.
HISTORY_PREFETCH_DAYS = 21
# Пороги (OI_WINDOW_DAYS_*/VOLUME_LOOKBACK_DAYS/KLINES_LIMIT/RSI_PERIOD/
# PUMP_THRESHOLD_PCT) — config.py, єдиний файл для
# ручного редагування (2026-10-03).


def _pct_change(old: float, new: float) -> Optional[float]:
    if old == 0:
        return None
    return (new - old) / abs(old) * 100


def pump_pct_from_aggregated(data: dict) -> Optional[float]:
    """24h-рух ціни для пампу — МАКСИМУМ (не середнє) серед бірж, де
    символ присутній. Живо виявлено 2026-09-27: середнє
    (`price_change_percent_avg`) розмиває реальний памп, якщо він
    проявився сильно лише на одній біржі (тонкий/ще не наздогнав
    лістинг на решті) — саме так користувач і читає сигнал (дивиться
    на конкретну біржу, не на середнє по трьох)."""
    changes = [
        ex_data["price_change_percent"]
        for ex_data in data["per_exchange"].values()
        if "price_change_percent" in ex_data
    ]
    return float(max(changes)) if changes else None


def compute_oi_change_pct(conn, symbol: str, window_days: int) -> Optional[float]:
    """Зміна OI за вікно — по ВСІХ біржах, де є узгоджена історія
    (history.py). До 2026-10-15 читалось лише з Bybit, і 201 символ із
    1056 отримував None назавжди — docs/decisions.md."""
    rows = fetch_history_rows(conn, symbol, KIND_OI, days=HISTORY_PREFETCH_DAYS)
    series = consistent_daily_series(rows, limit=window_days + 1)
    return oi_change_pct(series)


def compute_volume_spike_pct(conn, symbol: str) -> Optional[float]:
    """Сплеск обсягу проти середнього за вікно — по всіх біржах
    (та сама причина, що в compute_oi_change_pct)."""
    rows = fetch_history_rows(conn, symbol, KIND_VOLUME, days=HISTORY_PREFETCH_DAYS)
    series = consistent_daily_series(rows, limit=VOLUME_LOOKBACK_DAYS + 1)
    return volume_spike_pct(series)


def compute_price_indicators(session: requests.Session, symbol: str) -> dict:
    """RSI і підтвердження тренду (backtest) — з Binance klines напряму
    (не з нашої історії, докладніше docstring модуля). Повертає
    {"rsi": Optional[float], "trend_confirmed": bool}.

    Живо виявлено 2026-09-27: `pump_pct` РАНІШЕ рахувався тут-таки з
    5-денного вікна Binance klines — і мовчки пропускав реальні
    одноденні пампи (користувач вказав на живі приклади: QUSDT +83%,
    QNTUSDT +52%, SOONUSDT +46%, того самого дня, на Binance). Причина:
    5-денне вікно розмиває різкий одноденний рух, а для щойно-пампнутих
    монет часто ще й немає повних 15+ денних свічок для RSI/backtest
    узагалі — функція повертала б None задовго до pump_pct. Тепер
    `pump_pct` рахується в `run_screening()` з УЖЕ доступного
    агрегованого 24h-руху ціни (`aggregate_sources.py:price_change_percent_avg`)
    — того самого числа, яке кожна біржа сама показує як "24h change",
    доступного для КОЖНОГО Tier A-символу одразу, незалежно від того,
    чи є в нього повна історія на Binance."""
    closes = fetch_klines(session, symbol, limit=KLINES_LIMIT)
    if len(closes) < RSI_PERIOD + 1:
        return {"rsi": None, "trend_confirmed": False}

    values = [c["value"] for c in closes]
    rsi_value = rsi(values, period=RSI_PERIOD)

    observations_desc = list(reversed(closes))  # backtest_metric очікує найновіше перше
    backtest = backtest_metric(symbol, observations_desc, min_history=6)
    trend_confirmed = (
        backtest["trend_mae"] is not None
        and backtest["naive_mae"] is not None
        and backtest["trend_mae"] < backtest["naive_mae"]
    )

    return {"rsi": rsi_value, "trend_confirmed": trend_confirmed}


def compute_monitoring_indicators(session: requests.Session, symbol: str) -> dict:
    """RSI і сплеск обсягу на КОРОТШОМУ ТФ (MONITORING_KLINE_INTERVAL,
    4г) — для monitor_candidates.py (погодинний моніторинг УЖЕ
    активного кандидата), на відміну від compute_price_indicators()
    вище (денний ТФ, первинний скан/LONG). Рішення користувача
    2026-10-03: "денний ТФ підходить для глобальних висновків,
    моніторинг уже відібраних активів потрібно на меншому ТФ" — 4г дає
    стійкіший сигнал, ніж 1г (менше шуму), усе ще в ~6 разів швидший
    за денний.

    Повертає {"rsi": Optional[float], "volume_spike_pct": Optional[float]}.
    `volume_spike_pct` тут — остання 4г-свічка проти середнього за
    решту вікна (НЕ 24h rolling-показник біржі, на відміну від
    monitor_candidates.py-дельти "з минулого разу" для OI) — пряме
    вимірювання обсягу в коротшому вікні, не зсув добового."""
    candles = fetch_klines(
        session, symbol, limit=MONITORING_KLINES_LIMIT, interval=MONITORING_KLINE_INTERVAL
    )
    if len(candles) < RSI_PERIOD + 1:
        return {"rsi": None, "volume_spike_pct": None}

    values = [c["value"] for c in candles]
    rsi_value = rsi(values, period=RSI_PERIOD)

    volumes = [c["volume"] for c in candles]
    latest_volume = volumes[-1]
    baseline = volumes[:-1]
    volume_spike_pct = _pct_change(sum(baseline) / len(baseline), latest_volume) if baseline else None

    return {"rsi": rsi_value, "volume_spike_pct": volume_spike_pct}


def run_screening(limit: Optional[int] = None) -> dict[str, list]:
    session = requests.Session()

    binance_symbols = list_binance_symbols(session)
    bybit_symbols = list_bybit_symbols(session)
    okx_symbols = list_okx_symbols(session)

    binance_snapshot = fetch_binance_snapshot(session)
    bybit_snapshot = fetch_bybit_snapshot(session)
    okx_snapshot = fetch_okx_snapshot(session)

    aggregated = aggregate_snapshots(binance_snapshot, bybit_snapshot, okx_snapshot)

    oldest_onboard: dict[str, date] = {}
    for exchange, symbols in (
        ("binance_futures", binance_symbols),
        ("bybit_futures", bybit_symbols),
        ("okx_futures", okx_symbols),
    ):
        for entry in symbols:
            if entry["onboard_date"] is None:
                continue
            canonical = canonicalize_symbol(exchange, entry["symbol"])
            if canonical not in oldest_onboard or entry["onboard_date"] < oldest_onboard[canonical]:
                oldest_onboard[canonical] = entry["onboard_date"]

    today = datetime.now(timezone.utc).date()

    # Жива діра (2026-10-02, живий кейс користувача: MAGMAUSDT +40%
    # памп на Binance+Bybit, Tier A НЕ пройдений попри обсяг $241М і
    # сукупний OI ~$14М — далеко вище порогів). Причина: Binance
    # НІКОЛИ не дає OI в bulk-знімку (лише Bybit/OKX) — Binance-частка
    # OI додавалась (enrich_with_binance_oi нижче) лише ПІСЛЯ фільтра
    # Tier A, тобто тільки символам, що ВЖЕ пройшли й без неї. Якщо
    # сам Bybit+OKX OI нижче MIN_OPEN_INTEREST_USD (MAGMAUSDT: $4.4М
    # лише з Bybit, на OKX символу нема) — символ відсіювався Tier A
    # ще до того, як міг отримати шанс пройти з повним трибіржовим OI.
    # Фікс: для символів, що провалюють Tier A ЛИШЕ через OI
    # (підстановка штучно величезного OI у пробну копію — якщо це
    # змінює вердикт, OI справді єдина причина) і мають дані з
    # Binance — дістати Binance OI ЗАРАЗ (не для всього ринку, лише
    # для цих прикордонних) і додати в aggregated ПЕРЕД фінальним
    # Tier A. binance_oi_cache — щоб той самий символ не отримав
    # Binance OI ВДРУГЕ в enrich_with_binance_oi() нижче (там += ).
    binance_oi_cache: dict[str, Decimal] = {}
    for symbol, data in aggregated.items():
        if check_tier_a(symbol, data, oldest_onboard.get(symbol), today).eligible:
            continue
        if "binance_futures" not in data["per_exchange"]:
            continue
        boosted = dict(data, open_interest_usd=data["open_interest_usd"] + Decimal(10) ** 12)
        if not check_tier_a(symbol, boosted, oldest_onboard.get(symbol), today).eligible:
            continue  # провал Tier A НЕ лише через OI — Binance OI тут не допоможе

        try:
            oi_base = fetch_binance_oi(session, symbol)
            if oi_base is not None:
                oi_usd = oi_base * data["per_exchange"]["binance_futures"]["mark_price"]
                binance_oi_cache[symbol] = oi_usd
                data["open_interest_usd"] += oi_usd
        except Exception:
            logger.warning("%s: Binance OI (OI-прикордонний Tier A) не вдалось отримати", symbol, exc_info=True)
    if binance_oi_cache:
        logger.info("OI-прикордонні символи, врятовані Binance OI перед Tier A: %d", len(binance_oi_cache))

    eligible = [
        symbol
        for symbol, data in aggregated.items()
        if check_tier_a(symbol, data, oldest_onboard.get(symbol), today).eligible
    ]
    eligible.sort()
    total_eligible = len(eligible)
    logger.info("Tier A: %d/%d символів пройшли", total_eligible, len(aggregated))
    if limit:
        eligible = eligible[:limit]
        logger.info("--limit %d: обробляю %d з %d Tier A-виживших", limit, len(eligible), total_eligible)

    conn = get_connection()
    results: dict[str, list] = {"long": [], "short": [], "watch": []}
    try:
        # Binance OI — per-symbol, ЛИШЕ для Tier A-виживших (не для
        # всього ринку, той самий принцип, що сам адаптер документує).
        # Пропускаємо символи, уже збагачені вище (binance_oi_cache) —
        # інакше enrich_with_binance_oi() (+= нижче) додав би Binance
        # OI ВДРУГЕ.
        binance_oi_usd: dict[str, Decimal] = {}
        for symbol in eligible:
            if symbol in binance_oi_cache:
                continue
            binance_data = aggregated[symbol]["per_exchange"].get("binance_futures")
            if binance_data is None:
                continue
            try:
                oi_base = fetch_binance_oi(session, symbol)
                if oi_base is not None:
                    binance_oi_usd[symbol] = oi_base * binance_data["mark_price"]
            except Exception:
                logger.warning("%s: Binance OI не вдалось отримати", symbol, exc_info=True)
        enrich_with_binance_oi(aggregated, binance_oi_usd)

        # Зберегти щойно дістаний Binance OI (docs/decisions.md
        # 2026-10-15). Досі він використовувався для СЬОГОДНІШНЬОГО
        # рівня й викидався — тобто запити робились щодня, а історії по
        # них не було ніколи, і 133 символи, що торгуються лише на
        # Binance, не могли отримати oi_change_pct у принципі.
        # Нормалізація — в адаптері (rule 1), тут лише запис.
        try:
            oi_records = open_interest_records({**binance_oi_cache, **binance_oi_usd})
            if oi_records:
                stored = insert_observations(conn, oi_records)
                logger.info(
                    "Binance OI збережено: %d записів (%d нових/змінених)",
                    len(oi_records), stored,
                )
        except Exception:
            # Збій запису не має валити скринінг: вердикт по сьогоднішньому
            # дню рахується з живого знімка, історія лише накопичується.
            logger.error("Binance OI не вдалось зберегти", exc_info=True)
            conn.rollback()

        for symbol in eligible:
            try:
                data = aggregated[symbol]
                price = compute_price_indicators(session, symbol)
                oi_change_long = compute_oi_change_pct(conn, symbol, OI_WINDOW_DAYS_LONG)
                oi_change_short = compute_oi_change_pct(conn, symbol, OI_WINDOW_DAYS_SHORT)
                volume_spike = compute_volume_spike_pct(conn, symbol)
                pump_pct = pump_pct_from_aggregated(data)

                long_signal = screen_long(
                    symbol=symbol,
                    trend_confirmed=price["trend_confirmed"],
                    oi_change_pct=oi_change_long,
                    rsi_value=price["rsi"],
                    funding_rate=data["funding_rate_avg"],
                )
                if long_signal.qualifies:
                    # Проставляємо тут, не в screen_long() — чиста
                    # функція рішення отримує ці значення як аргументи,
                    # не повинна сама знати про персистування
                    # (long_screen.py docstring, rule 1 CLAUDE.md).
                    long_signal.oi_change_pct = oi_change_long
                    long_signal.rsi_value = price["rsi"]
                    long_signal.funding_rate = data["funding_rate_avg"]
                    results["long"].append(long_signal)

                short_watch_signal = screen_short_or_watch(
                    symbol=symbol,
                    pump_pct=pump_pct,
                    oi_change_pct_after_pump=oi_change_short,
                    volume_spike_pct=volume_spike,
                    rsi_value=price["rsi"],
                    funding_rate=data["funding_rate_avg"],
                )
                if short_watch_signal.category == "short":
                    results["short"].append(short_watch_signal)
                elif short_watch_signal.category == "watch":
                    results["watch"].append(short_watch_signal)

                # Будь-який памп ≥ порогу — у список кандидатів на
                # погодинний моніторинг (monitor_candidates.py), навіть
                # якщо денний прогін не зміг підтвердити SHORT/WATCH
                # через брак історії (docs/decisions.md, 2026-09-27:
                # "тримати монету, поки дані відповідають" — не
                # одноразовий висновок). upsert_candidate() сам не
                # чіпає вже активний рядок.
                if pump_pct is not None and pump_pct >= PUMP_THRESHOLD_PCT:
                    binance_data = data["per_exchange"].get("binance_futures")
                    upsert_candidate(
                        conn, symbol, pump_pct, data["funding_rate_avg"],
                        data["open_interest_usd"],
                        price=binance_data["mark_price"] if binance_data else None,
                    )
            except Exception:
                logger.error("%s: скринінг провалився", symbol, exc_info=True)

        # Один run_at на весь прогін (save_long_run()) — той самий
        # append-only-знімок принцип, що screening_results (акції).
        # Раніше LONG лише логувався нижче (main()), ніколи не йшов у
        # БД — reporting/crypto_long_notify.py (2026-10-02) читає звідси.
        saved_long = save_long_run(conn, results["long"])
        if saved_long:
            logger.info("Збережено LONG-кандидатів: %d", saved_long)
    finally:
        conn.close()

    return results


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="лише перші N Tier A-виживших (тест)")
    args = parser.parse_args()

    results = run_screening(limit=args.limit)

    logger.info(
        "Готово: %d лонг, %d шорт, %d спостереження",
        len(results["long"]), len(results["short"]), len(results["watch"]),
    )
    for category in ("long", "short", "watch"):
        for signal in results[category]:
            logger.info("[%s] %s — %s", category.upper(), signal.symbol, "; ".join(signal.reasons))


if __name__ == "__main__":
    main()
