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
from dataclasses import replace
import logging
import os
import sys
from datetime import datetime, timezone
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
from common import market_hours  # noqa: E402
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
    fetch_discovery_candidates,
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
    category_map,
    is_native_category,
    native_categories,
    watchlist_assets_for_category,
)
from trading_list.scoring import (  # noqa: E402
    REJECT_MARKET_CLOSED,
    REJECT_NO_CATALYST,
    CatalystHit,
    ScoreBreakdown,
    catalyst_score,
    combine,
    crypto_trend_score,
    is_today_catalyst,
    price_levels,
    quality_score,
    rejection_reason,
    resolve_direction,
    score_asset,
    select_final,
    trading_direction,
    trend_consistent,
    trend_score,
    why_line,
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


# `watchlist_assets.asset_class` → `trading_list.kind`. Доти тип
# виводився з asset_id (`_WATCHLIST_CRYPTO`/`_WATCHLIST_FX` на 5
# активів), і все, чого там не було, ставало "commodity" — живий кейс
# 2026-10-22: акцію SKYD показано як "товар". Невизначений клас —
# 'unknown', а не здогад.
def _watchlist_kind(asset: dict) -> str:
    return asset.get("asset_class") or "unknown"


def _collect_price_anomalies(conn, extra_ids: Optional[list[str]] = None) -> dict[str, dict]:
    """{asset_id: {direction, pct_change, window_days, history_points}}
    для watchlist-активів, чий рух НЕЗВИЧНИЙ за їхньою власною
    історією (`news_analysis/prices.py:is_anomalous_move`).

    Той самий детектор, що вже використовує `synthesize.py` для
    вирішення "чи є про що писати без новин" — тут він стає
    каталізатором. Переюзовуємо, а не пишемо свій: один поріг
    аномальності на весь агент.

    `extra_ids` — тикери скринінгу (2026-10-22, пункт 4 плану): доти
    детектор дивився лише на watchlist, і акція скринінгу з незвичним
    рухом сьогодні власної події не мала зовсім. Їхнє джерело ціни —
    конвенція Twelve Data (`prices.py:_resolve_price_source`)."""
    price_sources = fetch_price_sources(conn)
    asset_ids = sorted(set(price_sources) | set(extra_ids or []))
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
    # Які активи списку реагують на яку категорію — з КЛАСУ активу
    # (`categories.py:category_map`), не з поіменної мапи.
    watchlist = fetch_watchlist_universe(conn)
    cat_map = category_map(watchlist)
    natives = native_categories(watchlist)
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
            at=release["scheduled_at"],
        )

        event_key = f"release:{release['metric_id']}:{release['scheduled_at']:%Y%m%d%H%M}"
        for asset_id in watchlist_assets_for_category(category or "", cat_map):
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

            for asset_id in watchlist_assets_for_category(category, cat_map):
                # Напрямок зараховується ЛИШЕ з ВЛАСНОЇ категорії
                # активу (categories.py:is_native_category). Жива
                # причина (прогін 2026-10-04): усі 10 активів виходили
                # `conflicting`, бо "ставка down" зараховувалась як
                # голос ПРО СОЛАНУ — а це причина, не напрямок солани.
                own = direction if is_native_category(asset_id, category, natives) else "neutral"
                add(asset_id, CatalystHit(
                    "release_done", config.CATALYST_WEIGHT_RELEASE_MEDIUM, own, text,
                    metric_id=impact_row["metric_id"],
                    detail=str(impact.get("explanation") or "")[:300],
                    at=impact_row.get("created_at"),
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
            at=news.get("created_at"),
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
            at=datetime.combine(earnings["report_date"], datetime.min.time(), tzinfo=timezone.utc),
        ), f"earnings:{ticker}:{earnings['report_date']}")

    # --- синтез ціна↔новини ---
    for syn in fetch_synthesis_catalysts(
        conn, config.CATALYST_NEWS_HOURS, config.MIN_SYNTHESIS_CONFIDENCE
    ):
        add(syn["asset_id"], CatalystHit(
            "synthesis", config.CATALYST_WEIGHT_SYNTHESIS, syn["direction"],
            f"синтез ціна/новини: {syn['summary'][:120]}",
            detail=syn["summary"],
            at=syn.get("created_at"),
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
    stock_tickers = [s["ticker"] for s in fetch_stock_universe(conn)]
    for asset_id, anomaly in _collect_price_anomalies(conn, stock_tickers).items():
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

    # --- КРИПТО-СЕТАП як власна подія (2026-10-22, пункт 4 плану) ---
    # Свіжий сетап LONG/SHORT і є подією контракту сьогодні. 'watch' —
    # спостереження без напрямку, власним каталізатором не є.
    for coin in fetch_crypto_universe(conn):
        setup = coin["setup"]
        if setup == "watch":
            continue
        direction = "up" if setup == "long" else "down"
        add(coin["symbol"], CatalystHit(
            "crypto_setup", config.CATALYST_WEIGHT_CRYPTO_SETUP, direction,
            f"сетап {setup}: {coin.get('reason') or ''}".strip(" :"),
            detail=str(coin.get("reason") or ""),
            # Час ВИЯВЛЕННЯ, не останньої перевірки: монітор оновлює
            # `last_checked_at` щогодини, і сетап тижневої давності
            # інакше вічно був би "подією сьогодні".
            at=coin.get("detected_at"),
        ), f"crypto_setup:{coin['symbol']}")

    # --- НОВАЧКИ (discovery) як власна подія ---
    # Рух активу, якого ми ще не вели, — подія сьогодні; напрямок дає
    # сам рух (`discovery/`).
    for cand in fetch_discovery_candidates(conn, config.TRADING_TODAY_HOURS):
        add(cand["ticker"], CatalystHit(
            "discovery", config.CATALYST_WEIGHT_DISCOVERY, cand.get("direction") or "neutral",
            f"новачок ({cand.get('detector')}): {str(cand.get('reasoning') or '')[:120]}",
            detail=str(cand.get("reasoning") or ""),
            at=cand.get("discovered_at"),
        ), f"discovery:{cand['ticker']}")

    # --- НАШ прогноз (лише довірені показники) ---
    for forecast in fetch_trusted_forecasts(conn, config.FORECAST_TRUSTED_METRICS):
        category = _METRIC_CATEGORY.get(forecast["metric_id"])
        text = f"наш прогноз {forecast['metric_id']}: {forecast['direction']}"
        event_key = f"forecast:{forecast['metric_id']}:{forecast['based_on_observed_at']}"

        for asset_id in watchlist_assets_for_category(category or "", cat_map):
            own = (
                forecast["direction"]
                if is_native_category(asset_id, category or "", natives) else "neutral"
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


def build_list(
    conn, require_own_catalyst: Optional[bool] = None, now: Optional[datetime] = None,
) -> tuple[list[dict], dict]:
    """(рядки для `save_trading_list()`, підсумок прогону).

    З 2026-10-22 (пункт 4 плану сесії) — "ЧИМ ТОРГУВАТИ СЬОГОДНІ":
    вхід — УСІ наші списки (watchlist, скринінг акцій, крипто-сетапи,
    новачки), вихід — лише активи з ВЛАСНИМ каталізатором сьогодні й
    однозначним напрямком (`scoring.py:rejection_reason`). Кожен рядок
    несе рівні й коротке "чому". Підсумок — скільки оцінено й скільки
    відсіяно за якою причиною (+ приклади), щоб порожній список був
    поясненим, а не загадковим.

    `require_own_catalyst` — перекриває `config.STOCK_REQUIRE_OWN_CATALYST`
    (для порівняння двох режимів на тих самих даних без зміни
    продакшену)."""
    if require_own_catalyst is None:
        require_own_catalyst = config.STOCK_REQUIRE_OWN_CATALYST
    now = now or datetime.now(timezone.utc)
    per_asset, stock_floor = collect_catalysts(conn)
    logger.info(
        "Каталізатори: %d активів прицільно, %d широких макро для акцій",
        len(per_asset), len(stock_floor),
    )

    # Кандидат: (asset_id, breakdown, is_watchlist, meta). meta — kind,
    # source, label, клас для фільтра, ВЛАСНІ хіти, закриття, ціна.
    candidates: list[tuple[str, ScoreBreakdown, bool, dict]] = []
    rejected: dict[str, dict] = {}

    def reject(asset_id: str, reason: str) -> None:
        bucket = rejected.setdefault(reason, {"count": 0, "examples": []})
        bucket["count"] += 1
        if len(bucket["examples"]) < 5:
            bucket["examples"].append(asset_id)

    watchlist = fetch_watchlist_universe(conn)
    # Тикери акцій списку обраних: такий тикер оцінюється ОДИН раз — як
    # актив списку (бонус і зарезервовані слоти), а не вдруге як рядок
    # скринінгу.
    watchlist_tickers = {
        (a.get("ticker") or "").upper() for a in watchlist if a.get("asset_class") == "stock"
    }
    seen_ids: set[str] = {a["asset_id"].upper() for a in watchlist} | watchlist_tickers

    # --- акції скринінгу ---
    prefiltered = 0
    stocks = fetch_stock_universe(conn)
    best_composite = max((s["score"] for s in stocks), default=None)
    for stock in stocks:
        ticker = stock["ticker"]
        if ticker.upper() in watchlist_tickers:
            continue
        seen_ids.add(ticker.upper())
        own_hits = per_asset.get(ticker, [])
        if not own_hits:
            # Без ВЛАСНОЇ події акція в список для торгів не потрапляє:
            # спільне макро однакове для 215 тикерів і нічого не каже
            # про конкретний (`categories.py`, розділ про обмеження).
            reject(ticker, REJECT_NO_CATALYST)
            prefiltered += 1
            continue
        if require_own_catalyst and not any(h.kind in ("news", "synthesis") for h in own_hits):
            reject(ticker, REJECT_NO_CATALYST)
            prefiltered += 1
            continue
        closes = fetch_closes(conn, "twelvedata", f"{ticker.lower()}_close", config.TREND_LONG_DAYS + 5)
        breakdown = score_asset(
            values=closes, hits=own_hits + stock_floor,
            composite=stock["score"], best_composite=best_composite,
            is_stock=True,
        )
        candidates.append((ticker, breakdown, False, {
            "kind": "stock", "source": "screening", "class": "stock",
            # Назва компанії, якщо SEC EDGAR її дав — інакше сам тикер
            # (живий фідбек 2026-10-04: "XAGUSD"/"BRENT_CRUDE" у
            # повідомленні замість людських назв).
            "label": stock.get("company_name") or ticker,
            "own_hits": own_hits, "closes": closes, "last_price": None,
        }))

    # --- крипта (власний шар торгуємості) ---
    for coin in fetch_crypto_universe(conn):
        symbol = coin["symbol"]
        if symbol.upper() in seen_ids:
            continue
        seen_ids.add(symbol.upper())
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
        candidates.append((symbol, breakdown, False, {
            "kind": "crypto", "source": "crypto_screening", "class": "crypto", "label": symbol,
            "own_hits": hits, "closes": [], "last_price": coin.get("last_price"),
        }))

    # --- watchlist (пріоритет: бонус + зарезервовані слоти) ---
    for asset in watchlist:
        asset_id = asset["asset_id"]
        hits = list(per_asset.get(asset_id, []))
        if asset.get("asset_class") == "stock" and asset.get("ticker"):
            # Звітність (`earnings_calendar`) і новини по тикеру
            # ключуються ТИКЕРОМ ("SKYD"), а актив списку — asset_id
            # ("skyd"). Без цього акція з watchlist не бачила власних
            # каталізаторів узагалі (живий кейс 2026-10-22).
            seen_texts = {h.text for h in hits}
            hits += [h for h in per_asset.get(asset["ticker"].upper(), []) if h.text not in seen_texts]
        closes = fetch_closes(
            conn, asset["source"], asset["metric_id"], config.TREND_LONG_DAYS + 5
        )
        breakdown = score_asset(values=closes, hits=hits, is_watchlist=True)
        candidates.append((asset_id, breakdown, True, {
            "kind": _watchlist_kind(asset), "source": "watchlist", "class": asset.get("asset_class"),
            # `watchlist_assets.label` уже містить людську назву
            # ("Срібло (XAG/USD)", "Нафта Brent") — беремо її, не
            # asset_id.
            "label": asset.get("label") or asset_id,
            "own_hits": hits, "closes": closes, "last_price": None,
        }))

    # --- НОВАЧКИ (discovery): ще не в жодному нашому списку ---
    # Ті, що вже є в іншому списку, свою подію віддали через
    # `per_asset` (`collect_catalysts`) — тут лише нові.
    for cand in fetch_discovery_candidates(conn, config.TRADING_TODAY_HOURS):
        ticker = cand["ticker"]
        if ticker.upper() in seen_ids:
            continue
        seen_ids.add(ticker.upper())
        asset_class = cand.get("asset_class") or "stock"
        hits = per_asset.get(ticker, [])
        closes = (
            fetch_closes(conn, "twelvedata", f"{ticker.lower()}_close", config.TREND_LONG_DAYS + 5)
            if asset_class == "stock" else []
        )
        if len(closes) >= config.TREND_MIN_POINTS:
            t_score, t_direction, t_detail = trend_score(closes)
        else:
            # Історії немає (новачок) — напрямок і сила — з самого руху,
            # який його й виявив.
            t_score = min(max(Decimal(str(cand.get("score") or 0)), Decimal("0")), Decimal("1"))
            t_direction = cand.get("direction") or "unclear"
            t_detail = f"рух новачка ({cand.get('detector')})"
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
        candidates.append((ticker, breakdown, False, {
            "kind": asset_class, "source": "discovery", "class": asset_class,
            "label": cand.get("company_name") or ticker,
            "own_hits": hits, "closes": closes, "last_price": None,
        }))

    # --- ФІЛЬТР "ЧИМ ТОРГУВАТИ СЬОГОДНІ" ---
    passed: list[tuple[str, ScoreBreakdown, bool]] = []
    extras: dict[str, dict] = {}
    for asset_id, breakdown, is_watchlist, meta in candidates:
        if not market_hours.is_open(meta["class"], now):
            reject(asset_id, REJECT_MARKET_CLOSED)
            continue
        today_hits = [h for h in meta["own_hits"] if is_today_catalyst(h, meta["class"], now)]
        direction = trading_direction(
            today_hits, breakdown.trend_direction,
            trend_is_consistent=trend_consistent(meta["closes"]) if meta["closes"] else None,
        )
        reason = rejection_reason(today_hits, direction, breakdown.total)
        if reason is not None:
            reject(asset_id, reason)
            continue
        passed.append((asset_id, replace(breakdown, direction=direction), is_watchlist))
        extras[asset_id] = {
            **meta,
            "today_hits": today_hits,
            "levels": price_levels(meta["closes"], meta["last_price"]),
        }

    final = select_final(passed)
    # `evaluated` — усі, кого розглянули: кандидати + акції, відсіяні ще
    # до скорингу (без власної події).
    summary = {"evaluated": len(candidates) + prefiltered, "rejected": rejected}
    logger.info(
        "Оцінено %d, пройшло фільтр «сьогодні» %d, у список %d; відсіяно: %s",
        summary["evaluated"], len(passed), len(final),
        ", ".join(f"{r} — {b['count']}" for r, b in rejected.items()) or "—",
    )

    rows = []
    for asset_id, breakdown, _ in final:
        extra = extras[asset_id]
        # У причинах — ЛИШЕ сьогоднішні події (трейдеру потрібне те, що
        # рухає актив зараз) + тренд окремою причиною, щоб reporting/
        # міг сказати, що саме відбувається.
        reasons = [h.as_reason() for h in extra["today_hits"]]
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
            "label": extra["label"],
            "kind": extra["kind"],
            "direction": breakdown.direction,
            "score": breakdown.total,
            "catalyst_score": breakdown.catalyst,
            "trend_score": breakdown.trend,
            "quality_score": breakdown.quality,
            "catalyst_summary": breakdown.catalyst_summary,
            "reasons": json.dumps(reasons, ensure_ascii=False),
            "source": extra["source"],
            "levels": json.dumps(extra["levels"], ensure_ascii=False) if extra["levels"] else None,
            "why": why_line(extra["today_hits"], breakdown.trend_detail),
        })
    return rows, summary


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
        rows, summary = build_list(conn, require_own_catalyst=args.require_own_catalyst)

        for row in rows:
            logger.info(
                "%-12s %-5s score=%.3f (кат=%.2f тренд=%.2f якість=%.2f) %s/%s — %s | рівні %s",
                row["asset_id"], row["direction"], row["score"],
                row["catalyst_score"], row["trend_score"], row["quality_score"],
                row["kind"], row["source"], row["why"][:120], row["levels"],
            )
        for reason, bucket in summary["rejected"].items():
            logger.info("Відсіяно «%s»: %d (напр. %s)", reason, bucket["count"], ", ".join(bucket["examples"]))

        if args.dry_run:
            logger.info("--dry-run: у БД не записано")
            return

        # Записується й ПОРОЖНІЙ прогін (`trading_list_runs`): "сьогодні
        # торгувати нічим" — законна відповідь, і звіт мусить її бачити.
        inserted = save_trading_list(conn, rows, horizon=config.HORIZON, run_summary=summary)
        logger.info("Записано в trading_list: %d рядків (прогін записано в trading_list_runs)", inserted)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
