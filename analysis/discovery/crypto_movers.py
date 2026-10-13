"""
Детектор новачків серед крипто-ф'ючерсів — найширший ринок, до якого
агент має безкоштовний bulk-доступ (3 біржі, ~400 символів, кілька
HTTP-запитів на весь скан).

Відмінність від `crypto_screening/` (той самий вхідний знімок, інше
питання):
- `crypto_screening/` питає "чи тут ТОРГОВИЙ СЕТАП" — Tier A (OI, вік
  лістингу), RSI, funding, LONG/SHORT-правила;
- тут питання лише "чи тут щось РУХАЄТЬСЯ настільки, що варто
  подивитись" — тому Tier A свідомо НЕ застосовується (він відсіює за
  критеріями входу в позицію, а не за наявністю події), лишається
  тільки гейт від порожнього ринку за $-обсягом.

Рух рахується в ОБИДВІ сторони (рішення користувача 2026-10-05): -20%
такий самий кандидат, як +20%.
"""

import logging
from decimal import Decimal
from typing import Optional

from config import (
    DISCOVERY_CRYPTO_MIN_MOVE_PCT,
    DISCOVERY_CRYPTO_MIN_VOLUME_24H,
    DISCOVERY_CRYPTO_SATURATION_PCT,
    DISCOVERY_NEWS_SOURCE_REFERENCE,
    DISCOVERY_VOLUME_SPIKE_REFERENCE,
    DISCOVERY_W_MOVE,
    DISCOVERY_W_NEWS,
    DISCOVERY_W_VOLUME,
)
from discovery.signals import (
    Candidate,
    direction_from_move,
    score_candidate,
    volume_spike_pct,
)

logger = logging.getLogger(__name__)

# Канонічний суфікс символів ф'ючерсів (crypto_screening/
# aggregate_sources.py зводить усі 3 біржі до стилю "BTCUSDT").
#
# Символи з ІНШОЮ котирувальною валютою (BTCUSDC, ETHUSD) свідомо
# пропускаються: bulk-знімок Binance віддає й їх, а це ТОЙ САМИЙ актив
# під іншою назвою — без фільтра BTC приходив би "новачком" як
# BTCUSDC, попри те що BTC у watchlist. Монета, що має лише
# не-USDT-контракт, рідкість, і ціною за це свідомо платимо.
QUOTE_SUFFIX = "USDT"


def extreme_move_pct(aggregated_entry: dict) -> Optional[Decimal]:
    """24h-рух ціни — той з бірж, де він НАЙСИЛЬНІШИЙ ЗА МОДУЛЕМ.

    Не `aggregate_sources.pump_pct_from_aggregated()` (хоч вхід той
    самий): вона бере `max()`, бо шукає ПАМП — а для обвалу на -25% на
    трьох біржах max() вернув би найменш негативне значення й
    применшив би подію. Не середнє (`price_change_percent_avg`) — з тієї
    самої причини, що описана там: середнє розмиває рух, який сильно
    проявився лише на одній біржі."""
    changes = [
        Decimal(str(data["price_change_percent"]))
        for data in aggregated_entry.get("per_exchange", {}).values()
        if "price_change_percent" in data
    ]
    if not changes:
        return None
    return max(changes, key=abs)


def detect(
    aggregated: dict[str, dict],
    volume_history: dict[str, list[Decimal]],
    tracked: set[str],
) -> list[Candidate]:
    """Чиста функція (rule 1): приймає вже готовий агрегований знімок
    (`crypto_screening/aggregate_sources.py:aggregate_snapshots`) та
    історію обсягів із БД, не робить ні мережевих, ні SQL-викликів.

    `volume_history[SYMBOL]` — денні $-обсяги в хронологічному порядку
    (джерело — `bybit_futures`, єдина з 3 бірж, що віддає обсяг bulk, і
    тому єдина, чия історія в нас накопичується; символ без історії
    просто не отримує обсягового сигналу, не відкидається)."""
    candidates: list[Candidate] = []

    for symbol, data in aggregated.items():
        upper = symbol.upper()
        if not upper.endswith(QUOTE_SUFFIX):
            continue
        base = upper.removesuffix(QUOTE_SUFFIX)
        # Виключення — і за повним символом ("BTCUSDT" у крипто-скринінгу),
        # і за базовою монетою ("BTC" — так виглядає asset_id watchlist).
        if upper in tracked or base in tracked:
            continue

        if Decimal(str(data["quote_volume_24h"])) < DISCOVERY_CRYPTO_MIN_VOLUME_24H:
            continue

        move = extreme_move_pct(data)
        if move is None or abs(move) < DISCOVERY_CRYPTO_MIN_MOVE_PCT:
            continue

        # window=None — уся наявна історія Bybit (її й так лише
        # `DISCOVERY_VOLUME_HISTORY_DAYS` днів, і для щойно залистованих
        # символів вона коротша).
        spike = volume_spike_pct(volume_history.get(upper, []))
        score = score_candidate(
            move_pct=move,
            move_saturation_pct=DISCOVERY_CRYPTO_SATURATION_PCT,
            w_move=DISCOVERY_W_MOVE,
            w_volume=DISCOVERY_W_VOLUME,
            w_news=DISCOVERY_W_NEWS,
            volume_spike_pct=spike,
            volume_reference_pct=DISCOVERY_VOLUME_SPIKE_REFERENCE,
            news_source_count=0,
            news_source_reference=DISCOVERY_NEWS_SOURCE_REFERENCE,
        )

        exchanges = ", ".join(sorted(data.get("exchanges", [])))
        reason_parts = [
            f"рух {move:+.1f}% за 24г",
            f"обсяг ${Decimal(str(data['quote_volume_24h'])):,.0f}",
        ]
        if spike is not None:
            reason_parts.append(f"обсяг до середнього {spike:+.0f}%")
        if exchanges:
            reason_parts.append(f"біржі: {exchanges}")

        candidates.append(
            Candidate(
                asset_id=upper,
                label=f"{base}/USDT (ф'ючерс)",
                asset_class="crypto",
                detector="crypto_movers",
                direction=direction_from_move(move),
                reason="; ".join(reason_parts),
                signals={
                    "move_pct_24h": move,
                    "quote_volume_24h": data["quote_volume_24h"],
                    "volume_spike_pct": spike,
                    "exchange_count": data.get("exchange_count"),
                },
                score=score,
                source_refs=[{"type": "exchange_snapshot", "exchanges": sorted(data.get("exchanges", []))}],
            )
        )

    logger.info("crypto_movers: %d кандидатів з %d символів", len(candidates), len(aggregated))
    return candidates
