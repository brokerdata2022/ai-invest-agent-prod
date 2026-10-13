"""
Детектор новачків серед акцій — із ЦІН, ЩО ВЖЕ Є В БД, без жодного
нового запиту до API.

Чому так, а не "просканувати весь ринок": free tier Twelve Data це 800
запитів/добу, а щоденний збір S&P 500 їх майже вичерпує
(data-ingestion/quotes/twelvedata_adapter.py). Тож універсум тут — ті
тикери, по яких ціни вже збираються, і питання до них інше, ніж у
`screening/`: скринінг питає "яка компанія ХОРОША Й НЕДОРОГА"
(фундаментал, 215 рядків без жодної ознаки руху), а тут — "де щось
ВІДБУВАЄТЬСЯ прямо зараз", в обидві сторони.

Саме тому акції зі `screening_results` НЕ виключаються як "уже
відстежувані" (на відміну від watchlist і крипто-кандидатів,
`_db.py:fetch_tracked_identifiers`): різкий рух якісної акції — це
подія, якої жоден інший наш звіт не покаже.
"""

import logging
from decimal import Decimal
from typing import Optional

from config import (
    DISCOVERY_NEWS_SOURCE_REFERENCE,
    DISCOVERY_STOCK_MAX_DAILY_MOVE_PCT,
    DISCOVERY_STOCK_LONG_DAYS,
    DISCOVERY_STOCK_MIN_MOVE_PCT,
    DISCOVERY_STOCK_MIN_POINTS,
    DISCOVERY_STOCK_SATURATION_PCT,
    DISCOVERY_STOCK_SHORT_DAYS,
    DISCOVERY_VOLUME_SPIKE_REFERENCE,
    DISCOVERY_W_MOVE,
    DISCOVERY_W_NEWS,
    DISCOVERY_W_VOLUME,
)
from discovery.signals import (
    Candidate,
    direction_from_move,
    max_daily_move_pct,
    pct_change,
    score_candidate,
    volume_spike_pct,
)

logger = logging.getLogger(__name__)


def window_move_pct(closes: list[Decimal], days: int) -> Optional[Decimal]:
    """Рух за останні `days` ТОЧОК ряду (не календарних днів — ряд уже
    складається лише з торгових днів, вихідних у ньому немає). None,
    якщо точок бракує."""
    if len(closes) < days + 1:
        return None
    return pct_change(closes[-(days + 1)], closes[-1])


def detect(
    series: dict[str, dict],
    tracked: set[str],
    company_names: Optional[dict[str, str]] = None,
) -> list[Candidate]:
    """Чиста функція (rule 1): приймає вже прочитані ряди
    (`_db.py:fetch_stock_series`), сама до БД не звертається."""
    names = company_names or {}
    candidates: list[Candidate] = []

    for ticker, data in series.items():
        if ticker in tracked:
            continue

        closes = data.get("closes", [])
        if len(closes) < DISCOVERY_STOCK_MIN_POINTS:
            continue

        short_move = window_move_pct(closes, DISCOVERY_STOCK_SHORT_DAYS)
        if short_move is None or abs(short_move) < DISCOVERY_STOCK_MIN_MOVE_PCT:
            continue

        # Гейт правдоподібності (2026-10-05, живий кейс CTVA —
        # docs/decisions.md): одноденний стрибок понад
        # `DISCOVERY_STOCK_MAX_DAILY_MOVE_PCT` це майже завжди спліт/
        # спін-оф у нескоригованому ряду, а не подія на ринку. У ЛОГ з
        # назвою причини, не тихе відкидання — інакше наступний такий
        # випадок знову шукали б руками.
        window_closes = closes[-(DISCOVERY_STOCK_SHORT_DAYS + 1) :]
        daily_jump = max_daily_move_pct(window_closes)
        if daily_jump is not None and abs(daily_jump) > DISCOVERY_STOCK_MAX_DAILY_MOVE_PCT:
            logger.info(
                "%s: одноденний стрибок %+.1f%% — підозра на корпоративну дію "
                "(спліт/спін-оф у нескоригованому ряду), відкинуто",
                ticker,
                daily_jump,
            )
            continue

        long_move = window_move_pct(closes, DISCOVERY_STOCK_LONG_DAYS)
        spike = volume_spike_pct(data.get("volumes", []), DISCOVERY_STOCK_SHORT_DAYS)

        score = score_candidate(
            move_pct=short_move,
            move_saturation_pct=DISCOVERY_STOCK_SATURATION_PCT,
            w_move=DISCOVERY_W_MOVE,
            w_volume=DISCOVERY_W_VOLUME,
            w_news=DISCOVERY_W_NEWS,
            volume_spike_pct=spike,
            volume_reference_pct=DISCOVERY_VOLUME_SPIKE_REFERENCE,
            news_source_count=0,
            news_source_reference=DISCOVERY_NEWS_SOURCE_REFERENCE,
        )

        reason_parts = [f"рух {short_move:+.1f}% за {DISCOVERY_STOCK_SHORT_DAYS} торгових днів"]
        if long_move is not None:
            # Розбіжність знаків коротке/довге вікно — найцікавіший
            # випадок (розворот), і саме його видно лише якщо показати
            # обидва вікна, а не одне.
            reason_parts.append(f"за {DISCOVERY_STOCK_LONG_DAYS} днів {long_move:+.1f}%")
        if spike is not None:
            reason_parts.append(f"обсяг до середнього {spike:+.0f}%")

        candidates.append(
            Candidate(
                asset_id=ticker,
                label=names.get(ticker, ticker),
                asset_class="stock",
                detector="stock_movers",
                direction=direction_from_move(short_move),
                reason="; ".join(reason_parts),
                signals={
                    "move_pct_short": short_move,
                    "move_pct_long": long_move,
                    "max_daily_move_pct": daily_jump,
                    "volume_spike_pct": spike,
                    "points": len(closes),
                },
                score=score,
                source_refs=[{"type": "price_series", "source": "twelvedata", "ticker": ticker}],
            )
        )

    logger.info("stock_movers: %d кандидатів з %d тикерів у БД", len(candidates), len(series))
    return candidates
