#!/usr/bin/env python3
"""
Складає список активів, які варто РОЗГЛЯДАТИ для торгівлі
(`trading_list`) — дизайн і обґрунтування кожного рішення
**`docs/trading-list.md`**.

Не плутати з існуючими скринінгами: `screening/` відповідає "яка
компанія хороша й недорога" (215 рядків фундаменталу),
`crypto_screening/` — "який контракт має сетап". Тут поверх них
накладається ФІЛЬТР КАТАЛІЗАТОРА: актив потрапляє в список лише якщо з
ним щось відбувається (реліз/новина/наш прогноз) і сума компонентів
переступає поріг.

Межа шарів (rule 1, CLAUDE.md): цей файл — лише проводка (БД →
збирання каталізаторів → скоринг → БД). Пороги й ваги — `config.py`,
арифметика — `scoring.py` (чисті функції), мапа категорій —
`categories.py`. Жодного магічного числа тут.

Горизонт — `config.HORIZON` ('medium'). Інтрадей — закладене
розширення: інший профіль ваг + гейт "ринок відкритий у цю сесію"
(docs/trading-list.md), НЕ реалізований.

Використання:
    python run_trading_list.py
    python run_trading_list.py --dry-run    # порахувати й показати, не писати в БД
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Корінь репозиторію — для ЄДИНОГО `config.py` (рішення
# користувача 2026-10-04: один конфіг на весь агент).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))
sys.path.insert(0, _ANALYSIS_DIR)

from common.db import get_connection  # noqa: E402
from common.watchlist_db import fetch_price_sources  # noqa: E402
from news_analysis.prices import (  # noqa: E402
    fetch_all_price_changes,
    fetch_price_history,
    is_anomalous_move,
)

import config  # noqa: E402
from trading_list._db import (  # noqa: E402
    fetch_closes,
    fetch_crypto_universe,
    fetch_news_catalysts,
    fetch_recent_impacts,
    fetch_stock_universe,
    fetch_synthesis_catalysts,
    fetch_trusted_forecasts,
    fetch_upcoming_earnings,
    fetch_upcoming_releases,
    fetch_watchlist_universe,
    save_trading_list,
)
from trading_list.categories import (  # noqa: E402
    category_affects_stocks,
    is_native_category,
    watchlist_assets_for_category,
)
from trading_list.scoring import (  # noqa: E402
    CatalystHit,
    ScoreBreakdown,
    catalyst_score,
    crypto_trend_score,
    quality_score,
    resolve_direction,
    combine,
    score_asset,
    select_final,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _local_dt(value) -> str:
    """Час релізу в поясі користувача (`config.DISPLAY_TIMEZONE`).

    Той самий фікс, що `reporting/_common.py:format_local_dt` — але
    тут, бо `when` потрапляє в `trading_list.reasons` (JSONB) уже
    готовим рядком, і reporting/ його не переформатовує. Живий фідбек
    2026-10-05: у Telegram ішов UTC замість київського часу."""
    if not isinstance(value, datetime):
        return str(value) if value is not None else ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(ZoneInfo(config.DISPLAY_TIMEZONE)).strftime(
        config.DISPLAY_DATETIME_FORMAT
    )

_IMPACT_WEIGHT = {
    "high": config.CATALYST_WEIGHT_RELEASE_HIGH,
    "medium": config.CATALYST_WEIGHT_RELEASE_MEDIUM,
    "low": config.CATALYST_WEIGHT_RELEASE_LOW,
}

# metric_id → категорія впливу, щоб ЗАПЛАНОВАНИЙ реліз (який ще не
# вийшов, тож `asset_impacts` для нього не існує) теж міг стати
# каталізатором. Для вийшлих релізів категорії дає сам LLM
# (`expectation_synthesis.asset_impacts`) — тут лише груба відповідність
# наперед, свідомо консервативна.
_METRIC_CATEGORY = {
    "cpi": "ставка", "core_cpi": "ставка", "pce_price_index": "ставка",
    "fed_funds_rate": "ставка", "mortgage_rate_30y": "ставка",
    "treasury_10y": "ставка", "treasury_2y": "ставка",
    "nonfarm_payrolls": "економіка", "unemployment_rate": "економіка",
    "initial_jobless_claims": "економіка", "real_gdp": "економіка",
    "retail_sales": "економіка", "housing_starts": "економіка",
    "usdjpy_fx_rate": "валюта",
    "eurozone_hicp": "ставка", "eurozone_deposit_rate": "ставка",
    "eurozone_unemployment_rate": "економіка",
    "japan_policy_rate": "ставка", "japan_cpi": "ставка",
}


# asset_id watchlist → `kind` зі схеми (db/schema.sql: stock|crypto|fx|
# commodity). Watchlist змішаний: крипта, валютні пари й товари живуть
# в одній таблиці, тож тип виводимо з самого asset_id — інакше в
# `trading_list.kind` потрапляло б значення поза переліком схеми.
_WATCHLIST_CRYPTO = frozenset({"btc", "eth", "sol"})
_WATCHLIST_FX = frozenset({"eurusd", "usdjpy"})


def _watchlist_kind(asset_id: str) -> str:
    if asset_id in _WATCHLIST_CRYPTO:
        return "crypto"
    if asset_id in _WATCHLIST_FX:
        return "fx"
    return "commodity"


def _collect_price_anomalies(conn) -> dict[str, dict]:
    """{asset_id: {direction, pct_change, window_days, history_points}}
    для watchlist-активів, чий рух НЕЗВИЧНИЙ за їхньою власною
    історією (`news_analysis/prices.py:is_anomalous_move`).

    Той самий детектор, що вже використовує `synthesize.py` для
    вирішення "чи є про що писати без новин" — тут він стає
    каталізатором. Переюзовуємо, а не пишемо свій: один поріг
    аномальності на весь агент."""
    price_sources = fetch_price_sources(conn)
    asset_ids = sorted(price_sources)
    if not asset_ids:
        return {}

    window_days = config.CATALYST_ANOMALY_WINDOW_DAYS
    changes = fetch_all_price_changes(
        conn, asset_ids, days=window_days, price_sources=price_sources,
    )

    out: dict[str, dict] = {}
    for asset_id, change in changes.items():
        history = fetch_price_history(conn, asset_id, price_sources=price_sources)
        if not is_anomalous_move(change.pct_change, history, window_days):
            continue
        out[asset_id] = {
            "direction": "up" if change.pct_change > 0 else "down",
            "pct_change": change.pct_change,
            "window_days": window_days,
            "history_points": len(history),
        }
    if out:
        logger.info(
            "Аномальні рухи (власний каталізатор): %s",
            ", ".join(f"{a} {v['pct_change']:+.1f}%" for a, v in out.items()),
        )
    return out


def collect_catalysts(conn) -> tuple[dict[str, list[CatalystHit]], list[CatalystHit]]:
    """({asset_id/тикер → список каталізаторів}, спільна "підлога" для
    акцій).

    Два виходи, бо каталізатори поділяються за здатністю РІЗНИТИ
    активи: прицільні (новина про конкретний тикер, реліз у категорію
    конкретного watchlist-активу) і широкі макро, які стосуються всього
    ринку акцій одразу й місця в топі не визначають
    (`categories.py`, розділ про обмеження)."""
    per_asset: dict[str, list[CatalystHit]] = {}
    stock_floor: list[CatalystHit] = []
    # stock_floor потребує СВОГО дедупу: `add()` нижче дедупить лише
    # прицільні хіти на актив, а підлога збиралась простим append —
    # залишкова течія того самого бага (живий вивід 2026-10-04 показав
    # "unemployment_rate вийшов" ТРИЧІ в одному рядку акції: по разу на
    # категорію "ставка"/"економіка"/"акції").
    floor_seen: set[str] = set()

    def add_floor(hit: CatalystHit, event_key: str) -> None:
        if event_key in floor_seen:
            return
        floor_seen.add(event_key)
        stock_floor.append(hit)
    # Ключі вже зарахованих подій на актив — ДЕДУП. Жива причина
    # (прогін 2026-10-04): один реліз мапиться на `sol` через ТРИ
    # категорії ("ставка"/"акції"/"крипта") і зараховувався тричі, що
    # видно було прямо в тексті причин ("unemployment_rate вийшов"
    # двічі поспіль).
    seen: set[tuple[str, str]] = set()

    def add(asset_id: str, hit: CatalystHit, event_key: str) -> None:
        key = (asset_id, event_key)
        if key in seen:
            return
        seen.add(key)
        per_asset.setdefault(asset_id, []).append(hit)

    # --- заплановані релізи ---
    for release in fetch_upcoming_releases(conn, config.CATALYST_UPCOMING_DAYS):
        weight = _IMPACT_WEIGHT.get(release["impact_level"], config.CATALYST_WEIGHT_RELEASE_LOW)
        category = _METRIC_CATEGORY.get(release["metric_id"])
        when = _local_dt(release["scheduled_at"])
        text = f"реліз {release['metric_id']} {when}"
        hit = CatalystHit(
            "release_upcoming", weight, "neutral", text,
            metric_id=release["metric_id"],
            when=when,
        )

        event_key = f"release:{release['metric_id']}:{release['scheduled_at']:%Y%m%d%H%M}"
        for asset_id in watchlist_assets_for_category(category or ""):
            add(asset_id, hit, event_key)
        if category and category_affects_stocks(category):
            add_floor(hit, event_key)

    # --- вийшлі релізи: міжактивний вплив від LLM ---
    for impact_row in fetch_recent_impacts(conn, config.CATALYST_RECENT_DAYS):
        for impact in impact_row["asset_impacts"] or []:
            if not isinstance(impact, dict):
                continue
            category = str(impact.get("category") or "")
            direction = impact.get("direction") or "neutral"
            text = f"{impact_row['metric_id']} вийшов: {category} {direction}"
            event_key = f"impact:{impact_row['metric_id']}:{impact_row['observed_at']}"

            for asset_id in watchlist_assets_for_category(category):
                # Напрямок зараховується ЛИШЕ з ВЛАСНОЇ категорії
                # активу (categories.py:is_native_category). Жива
                # причина (прогін 2026-10-04): усі 10 активів виходили
                # `conflicting`, бо "ставка down" зараховувалась як
                # голос ПРО СОЛАНУ — а це причина, не напрямок солани.
                own = direction if is_native_category(asset_id, category) else "neutral"
                add(asset_id, CatalystHit(
                    "release_done", config.CATALYST_WEIGHT_RELEASE_MEDIUM, own, text,
                    metric_id=impact_row["metric_id"],
                    detail=str(impact.get("explanation") or "")[:300],
                ), event_key)

            if category_affects_stocks(category):
                stock_direction = (
                    direction if is_native_category("", category, is_stock=True) else "neutral"
                )
                add_floor(CatalystHit(
                    "release_done", config.CATALYST_WEIGHT_RELEASE_MEDIUM,
                    stock_direction, text,
                    metric_id=impact_row["metric_id"],
                    detail=str(impact.get("explanation") or "")[:300],
                ), f"{event_key}:stocks")

    # --- новини (РІЗНЯТЬ активи, включно з тикерами акцій) ---
    for news in fetch_news_catalysts(
        conn, config.CATALYST_NEWS_HOURS, config.MIN_NEWS_SOURCE_COUNT
    ):
        add(news["asset_id"], CatalystHit(
            "news", config.CATALYST_WEIGHT_NEWS, news["direction"],
            f"новина ({news['source_count']} джерел): {news['summary'][:120]}",
            detail=news["summary"], when=str(news["source_count"]),
        ), f"news:{news['asset_id']}:{news['summary'][:40]}")

    # --- ЗВІТНІСТЬ КОМПАНІЇ (власна подія тикера, не макрофон) ---
    #
    # Додано 2026-10-15. Доти дат звітності в агента не було взагалі:
    # EPS використовувався як цифра в Tier B, але "коли вийде наступний
    # звіт" не знав ніхто. Живий замір того дня: 252 з 512 наших
    # тикерів звітують у найближчі 14 днів — тобто половина універсуму
    # мала розкладений наперед каталізатор, який нікуди не підключався.
    #
    # Вага = CATALYST_WEIGHT_EARNINGS (0.7, як у новини), і хіт
    # СВІДОМО не приглушується MACRO_DAMPING: приглушення існує для
    # подій, що НЕ різнять активи (глобальний макрореліз), а звіт
    # стосується рівно одного тикера. Напрямок — neutral: дата відома,
    # результат ні (той самий принцип, що release_upcoming).
    for earnings in fetch_upcoming_earnings(conn, config.CATALYST_EARNINGS_WINDOW_DAYS):
        ticker = earnings["ticker"]
        when = earnings["report_date"].strftime("%d.%m")
        eps = earnings["eps_forecast"]
        text = (
            f"звіт {ticker} {when}"
            + (f" ({earnings['report_time']})" if earnings["report_time"] else "")
            + (f", прогноз EPS {eps}" if eps is not None else "")
        )
        add(ticker, CatalystHit(
            "earnings", config.CATALYST_WEIGHT_EARNINGS, "neutral", text,
            metric_id=ticker,
            detail=f"прогноз EPS {eps}" if eps is not None else "",
            when=when,
        ), f"earnings:{ticker}:{earnings['report_date']}")

    # --- синтез ціна↔новини ---
    for syn in fetch_synthesis_catalysts(
        conn, config.CATALYST_NEWS_HOURS, config.MIN_SYNTHESIS_CONFIDENCE
    ):
        add(syn["asset_id"], CatalystHit(
            "synthesis", config.CATALYST_WEIGHT_SYNTHESIS, syn["direction"],
            f"синтез ціна/новини: {syn['summary'][:120]}",
            detail=syn["summary"],
        ), f"synthesis:{syn['asset_id']}")

    # --- ВЛАСНИЙ аномальний рух ціни (РІЗНИТЬ активи ідеально) ---
    #
    # Доданий 2026-10-13. Задача була "кожен актив мусить мати власний
    # каталізатор", і замір показав, що через новини це недосяжно: за
    # 48 годин 0 із 196 консолідованих історій мали `asset_id` (GDELT
    # у бані, RSS дає широкі новини, не активо-специфічні). Аномальний
    # рух ЦІНИ активу — теж власна подія, і рахується з власної
    # історії кожного, тож різнить активи за визначенням.
    #
    # Стало можливим лише зараз: `is_anomalous_move` вимагає 10 денних
    # дохідностей, а до добору історії (yahoo_chart, 2026-10-12) у
    # товарів було 6-7 точок — детектор завжди повертав False.
    for asset_id, anomaly in _collect_price_anomalies(conn).items():
        add(asset_id, CatalystHit(
            "price_anomaly", config.CATALYST_WEIGHT_PRICE_ANOMALY,
            anomaly["direction"],
            f"аномальний рух: {anomaly['pct_change']:+.2f}% за {anomaly['window_days']} дн "
            f"(незвично для цього активу за його власною історією)",
            detail=(
                f"{anomaly['pct_change']:+.2f}% за {anomaly['window_days']} днів; "
                f"історія {anomaly['history_points']} точок"
            ),
        ), f"anomaly:{asset_id}")

    # --- НАШ прогноз (лише довірені показники) ---
    for forecast in fetch_trusted_forecasts(conn, config.FORECAST_TRUSTED_METRICS):
        category = _METRIC_CATEGORY.get(forecast["metric_id"])
        text = f"наш прогноз {forecast['metric_id']}: {forecast['direction']}"
        event_key = f"forecast:{forecast['metric_id']}:{forecast['based_on_observed_at']}"

        for asset_id in watchlist_assets_for_category(category or ""):
            own = (
                forecast["direction"]
                if is_native_category(asset_id, category or "") else "neutral"
            )
            add(asset_id, CatalystHit(
                "forecast", config.CATALYST_WEIGHT_FORECAST, own, text,
                metric_id=forecast["metric_id"],
                # `direction` — ГОЛОС про актив (neutral для чужої
                # категорії), `detail` — напрямок самого ПОКАЗНИКА.
                # Живий фідбек 2026-10-04: у повідомленні виходило
                # "наш прогноз CPI — без напрямку" у 8 рядках із 10,
                # бо показувався голос, а не прогноз.
                detail=forecast["direction"],
            ), event_key)

        if category and category_affects_stocks(category):
            add_floor(CatalystHit(
                "forecast", config.CATALYST_WEIGHT_FORECAST, "neutral", text,
                metric_id=forecast["metric_id"], detail=forecast["direction"],
            ), f"{event_key}:stocks")

    return per_asset, stock_floor


def build_list(conn, require_own_catalyst: Optional[bool] = None) -> list[dict]:
    """Повертає рядки, готові для `save_trading_list()`.

    `require_own_catalyst` — перекриває `config.STOCK_REQUIRE_OWN_CATALYST`
    (для порівняння двох режимів на тих самих даних без зміни
    продакшену)."""
    if require_own_catalyst is None:
        require_own_catalyst = config.STOCK_REQUIRE_OWN_CATALYST
    per_asset, stock_floor = collect_catalysts(conn)
    logger.info(
        "Каталізатори: %d активів прицільно, %d широких макро для акцій",
        len(per_asset), len(stock_floor),
    )

    scored: list[tuple[str, ScoreBreakdown, bool]] = []
    meta: dict[str, dict] = {}

    # --- акції ---
    stocks = fetch_stock_universe(conn)
    best_composite = max((s["score"] for s in stocks), default=None)
    for stock in stocks:
        ticker = stock["ticker"]
        own_hits = per_asset.get(ticker, [])
        hits = own_hits + stock_floor
        if not hits:
            # Без жодного каталізатора акція в торговий список не
            # потрапляє — саме це відсікає 215 рядків скринінгу до
            # дієздатного топу (docs/trading-list.md).
            continue
        if require_own_catalyst and not any(
            h.kind in ("news", "synthesis") for h in own_hits
        ):
            # Строгий режим (config.STOCK_REQUIRE_OWN_CATALYST):
            # макро-фон однаковий у всіх акцій і нічого не різнить, тож
            # без ВЛАСНОЇ новини/синтезу тикер у "що торгувати" зайвий.
            continue
        closes = fetch_closes(conn, "twelvedata", f"{ticker.lower()}_close", config.TREND_LONG_DAYS + 5)
        breakdown = score_asset(
            values=closes, hits=hits,
            composite=stock["score"], best_composite=best_composite,
            is_stock=True,
        )
        scored.append((ticker, breakdown, False))
        meta[ticker] = {
            "kind": "stock", "source": "screening",
            # Назва компанії, якщо SEC EDGAR її дав — інакше сам тикер
            # (живий фідбек 2026-10-04: "XAGUSD"/"BRENT_CRUDE" у
            # повідомленні замість людських назв).
            "label": stock.get("company_name") or ticker,
        }

    # --- крипта (власний шар торгуємості) ---
    for coin in fetch_crypto_universe(conn):
        symbol = coin["symbol"]
        hits = per_asset.get(symbol, [])
        t_score, t_direction, t_detail = crypto_trend_score(
            coin["setup"],
            oi_change_pct=coin.get("oi_change_pct"),
            pump_pct=coin.get("last_pump_pct") or coin.get("pump_pct_at_detection"),
        )
        c_score, c_summary = catalyst_score(hits) if hits else (Decimal("0"), "")
        q_score = quality_score(None, None)
        breakdown = ScoreBreakdown(
            total=combine(c_score, t_score, q_score, is_watchlist=False),
            catalyst=c_score, trend=t_score, quality=q_score,
            direction=resolve_direction([h.direction for h in hits] + [t_direction]),
            catalyst_summary="; ".join(p for p in (c_summary, t_detail) if p),
            reasons=[h.as_reason() for h in hits],
            trend_direction=t_direction,
            trend_detail=t_detail,
        )
        scored.append((symbol, breakdown, False))
        meta[symbol] = {"kind": "crypto", "source": "crypto_screening", "label": symbol}

    # --- watchlist (пріоритет: бонус + зарезервовані слоти) ---
    for asset in fetch_watchlist_universe(conn):
        asset_id = asset["asset_id"]
        hits = per_asset.get(asset_id, [])
        closes = fetch_closes(
            conn, asset["source"], asset["metric_id"], config.TREND_LONG_DAYS + 5
        )
        breakdown = score_asset(values=closes, hits=hits, is_watchlist=True)
        scored.append((asset_id, breakdown, True))
        meta[asset_id] = {
            "kind": _watchlist_kind(asset_id), "source": "watchlist",
            # `watchlist_assets.label` уже містить людську назву
            # ("Срібло (XAG/USD)", "Нафта Brent") — беремо її, не
            # asset_id.
            "label": asset.get("label") or asset_id,
        }

    final = select_final(scored)
    logger.info("Оцінено %d кандидатів, у список пройшло %d", len(scored), len(final))

    rows = []
    for asset_id, breakdown, _ in final:
        # Тренд іде в `reasons` ОКРЕМОЮ причиною (kind='trend') — щоб
        # reporting/ міг сказати, що саме суперечить, а не лишати
        # марну позначку "суперечливо" без розшифровки.
        reasons = list(breakdown.reasons)
        if breakdown.trend_detail:
            reasons.append({
                "kind": "trend",
                "direction": breakdown.trend_direction,
                "metric_id": "",
                "detail": breakdown.trend_detail,
                "when": "",
            })

        rows.append({
            "asset_id": asset_id,
            "label": meta[asset_id]["label"],
            "kind": meta[asset_id]["kind"],
            "direction": breakdown.direction,
            "score": breakdown.total,
            "catalyst_score": breakdown.catalyst,
            "trend_score": breakdown.trend,
            "quality_score": breakdown.quality,
            "catalyst_summary": breakdown.catalyst_summary,
            "reasons": json.dumps(reasons, ensure_ascii=False),
            "source": meta[asset_id]["source"],
        })
    return rows


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="порахувати й показати список, не записуючи в БД",
    )
    parser.add_argument(
        "--min-score", type=str, default=None,
        help="перекрити config.MIN_SCORE для цього прогону (калібрування)",
    )
    parser.add_argument(
        "--require-own-catalyst", action="store_true", default=None,
        help="строгий режим: акція без ВЛАСНОЇ новини/синтезу не входить "
             "(перекриває config.STOCK_REQUIRE_OWN_CATALYST для цього прогону)",
    )
    args = parser.parse_args()

    if args.min_score is not None:
        # Калібрувальний перекрив: config лишається джерелом істини,
        # прапорець потрібен лише щоб порівняти пороги на ТИХ САМИХ
        # живих даних, не редагуючи файл між прогонами.
        config.MIN_SCORE = Decimal(args.min_score)
        logger.info("MIN_SCORE перекрито на %s для цього прогону", config.MIN_SCORE)

    conn = get_connection()
    try:
        rows = build_list(conn, require_own_catalyst=args.require_own_catalyst)

        for row in rows:
            logger.info(
                "%-12s %-16s score=%.3f (кат=%.2f тренд=%.2f якість=%.2f) %s — %s",
                row["asset_id"], row["direction"], row["score"],
                row["catalyst_score"], row["trend_score"], row["quality_score"],
                row["kind"], row["catalyst_summary"][:100],
            )

        if args.dry_run:
            logger.info("--dry-run: у БД не записано")
            return

        inserted = save_trading_list(conn, rows, horizon=config.HORIZON)
        if inserted == 0:
            logger.info(
                "Жоден актив не пройшов поріг %s — нічого не записано "
                "(попередній список не затирається)", config.MIN_SCORE,
            )
        else:
            logger.info("Записано в trading_list: %d рядків", inserted)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
