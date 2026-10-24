"""
ПОВНИЙ РОЗБІР АКТИВУ НА ЗАПИТ `/ask` (2026-10-24, рішення користувача:
"лишаємо загальний аналіз, а розбір по чинниках я хочу бачити через
ask").

**Що змінилось того ж дня.** О 14:10 автоматично приходить лише
загальний вердикт (`fundamental_notify.py`). Розгорнутий розбір активу
— по чинниках, за і проти — раніше приходив сам; тепер — коли
користувач просить: «повний аналіз золота», «розбір BX».

**Чому не звичайна відповідь `/ask`.** Звичайна відповідь — 8 речень,
які модель складає з контексту (ціна, новини, синтез), і розбору за
чинниками активу там немає взагалі: живий запит «дай мені повний аналіз
на золото» 24.10 дав переказ цін і новин. Розбір уже є в
`fundamental_analysis`, у тому форматі, до якого користувач звик, —
його й треба показати.

**Свіжість.** Перед показом аналіз проганяється через той самий гейт
змін, що й щоденний (`analysis/change_gate.py`): з'явились нові дані —
DeepSeek робить новий розбір; ні — показується останній із датою. Тобто
відповідь ніколи не стара й не платить за повтор.
"""

import logging
import re
from typing import Optional
from zoneinfo import ZoneInfo

import config
from assistant import _db as assistant_db
from assistant import resolve
from fundamental._db import (
    fetch_latest_full_analysis,
    fetch_stock_for_analysis,
    fetch_watchlist_assets,
)
from fundamental.run_analysis import analyze_stock, analyze_watchlist_asset

logger = logging.getLogger(__name__)


def wants_full_analysis(question: str) -> bool:
    text = question.lower()
    return any(re.search(pattern, text) for pattern in config.ASSISTANT_FULL_ANALYSIS_PATTERNS)


def _refresh(conn, analyze, *args) -> None:
    """Прогнати аналіз через гейт змін. Збій — не привід мовчати:
    показується останній збережений розбір (з датою)."""
    try:
        status = analyze(conn, *args)
        logger.info("Повний розбір: аналіз %s — %s", args[0].get("asset_id") or args[0].get("ticker"), status)
    except Exception:
        logger.exception("Повний розбір: свіжий аналіз не вдався — показую останній збережений")
        conn.rollback()


def _with_date(row: dict, text: str) -> str:
    created = row["created_at"]
    stamp = (
        created.astimezone(ZoneInfo(config.DISPLAY_TIMEZONE)).strftime("%d.%m %H:%M")
        if hasattr(created, "astimezone") else str(created)
    )
    return f"{text}\n<i>🕒 Висновок від {stamp}; новий робиться, щойно змінюються дані.</i>"


def full_analysis_reply(conn, question: str, format_asset_message) -> Optional[str]:
    """HTML-текст розгорнутих розборів або None (питання не про
    повний аналіз, або актив не розпізнано, або розбору немає — тоді
    `/ask` відповідає звичайно).

    `format_asset_message` передається викликачем із
    `reporting/fundamental_notify.py` — той самий вигляд, що в
    автоматичних повідомленнях до 2026-10-24."""
    if not wants_full_analysis(question):
        return None
    assets = resolve.resolve_assets(question, assistant_db.asset_terms(conn), config.ASSISTANT_MAX_ASSETS)
    tickers = resolve.resolve_tickers(question, assistant_db.known_tickers(conn), config.ASSISTANT_MAX_ASSETS)
    if not assets and not tickers:
        return None

    watchlist = fetch_watchlist_assets(conn)
    by_id = {a["asset_id"]: a for a in watchlist}
    covered_tickers = {(by_id[a].get("ticker") or "").upper() for a in assets if a in by_id}

    targets: list[str] = []
    for asset_id in assets:
        if asset_id in by_id:
            _refresh(conn, analyze_watchlist_asset, by_id[asset_id], watchlist)
            targets.append(asset_id)
    for ticker in tickers:
        if ticker.upper() in covered_tickers:
            continue
        _refresh(conn, analyze_stock, fetch_stock_for_analysis(conn, ticker))
        targets.append(ticker.upper())

    messages = []
    for asset_id in targets:
        row = fetch_latest_full_analysis(conn, asset_id)
        if row is not None:
            messages.append(_with_date(row, format_asset_message(row)))
    return "\n\n".join(messages) if messages else None
