"""
Адаптер Binance USDⓈ-M Futures public API — широкий ринок безстрокових
ф'ючерсів (НЕ закритий watchlist `crypto/binance_adapter.py`,
docs/watchlist.md) для крипто-скринінгу лонг/шорт/спостереження
(docs/archive/plan-journal-2026-10-11.md, Фаза 4; методологія узгоджена з користувачем — докладніше
docs/decisions.md, 2026-09-27).
https://binance-docs.github.io/apidocs/futures/en/

Крок 1 з 5 поетапної побудови (docs/decisions.md): лише Binance зараз —
Bybit/OKX, той самий інтерфейс і агрегація, крок 2.

Ключова відмінність від `binance_adapter.py` (METRICS — фіксований
словник watchlist, один symbol на виклик): тут UNIVERSE — увесь ринок,
не 3 монети. `list_perpetual_symbols()`/`fetch_market_snapshot()` —
bulk-ендпоінти (весь ринок ОДНИМ запитом, той самий принцип, що фікс
N+1-запитів у Tier A акцій, docs/decisions.md 2026-09-25) — дешевий
перший прохід ДО дорожчих запитів. `fetch_open_interest()` —
per-symbol (Binance не має bulk-ендпоінта для Open Interest), тому
викликається лише для symbols, що вже пройшли Tier A-фільтр (крок 4
воронки), не для всього ринку одразу.

Live-запит цього API живо цією сесією НЕ підтверджено (той самий
чесний підхід, що xagusd-проксі, docs/watchlist.md) — форма відповіді
взята з офіційної документації Binance Futures, потребує підтвердження
першим реальним запуском користувача.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

FUTURES_BASE_URL = "https://fapi.binance.com"
EXCHANGE_INFO_URL = f"{FUTURES_BASE_URL}/fapi/v1/exchangeInfo"
TICKER_24HR_URL = f"{FUTURES_BASE_URL}/fapi/v1/ticker/24hr"
PREMIUM_INDEX_URL = f"{FUTURES_BASE_URL}/fapi/v1/premiumIndex"
OPEN_INTEREST_URL = f"{FUTURES_BASE_URL}/fapi/v1/openInterest"
KLINES_URL = f"{FUTURES_BASE_URL}/fapi/v1/klines"

QUOTE_ASSET = "USDT"


def list_perpetual_symbols(session: requests.Session) -> list[dict]:
    """Активні безстрокові USDT-контракти з exchangeInfo — universe для
    скринінгу (не фіксований словник, на відміну від METRICS у
    binance_adapter.py). Повертає
    [{"symbol": "BTCUSDT", "onboard_date": date(...) | None}, ...]."""
    response = session.get(EXCHANGE_INFO_URL, timeout=30)
    response.raise_for_status()
    payload = response.json()

    symbols = []
    for entry in payload.get("symbols", []):
        if (
            entry.get("status") == "TRADING"
            and entry.get("contractType") == "PERPETUAL"
            and entry.get("quoteAsset") == QUOTE_ASSET
        ):
            onboard_ms = entry.get("onboardDate")
            onboard_date = (
                datetime.fromtimestamp(onboard_ms / 1000, tz=timezone.utc).date()
                if onboard_ms
                else None
            )
            symbols.append({"symbol": entry["symbol"], "onboard_date": onboard_date})
    return symbols


def fetch_market_snapshot(session: requests.Session) -> dict[str, dict]:
    """Bulk-знімок усього ринку — 2 HTTP-запити (24hr ticker + premiumIndex),
    НЕ по одному на symbol (той самий принцип, що фікс N+1 у Tier A
    акцій, docs/decisions.md 2026-09-25). Повертає
    {symbol: {quote_volume, price_change_percent, funding_rate, mark_price}}
    — лише symbols, присутні в ОБОХ відповідях (funding rate публікується
    тільки для активних безстрокових контрактів, той самий фільтр, що
    exchangeInfo)."""
    ticker_response = session.get(TICKER_24HR_URL, timeout=30)
    ticker_response.raise_for_status()
    ticker_payload = ticker_response.json()

    premium_response = session.get(PREMIUM_INDEX_URL, timeout=30)
    premium_response.raise_for_status()
    premium_payload = premium_response.json()

    funding_by_symbol: dict[str, dict] = {}
    for entry in premium_payload:
        symbol = entry.get("symbol")
        if not symbol:
            continue
        try:
            funding_by_symbol[symbol] = {
                "funding_rate": Decimal(entry["lastFundingRate"]),
                "mark_price": Decimal(entry["markPrice"]),
            }
        except (KeyError, InvalidOperation, TypeError):
            logger.warning("Binance Futures premiumIndex: пропущено %r — неповні дані", entry)

    snapshot: dict[str, dict] = {}
    for entry in ticker_payload:
        symbol = entry.get("symbol")
        if not symbol or symbol not in funding_by_symbol:
            continue
        try:
            snapshot[symbol] = {
                "quote_volume": Decimal(entry["quoteVolume"]),
                "price_change_percent": Decimal(entry["priceChangePercent"]),
                **funding_by_symbol[symbol],
            }
        except (KeyError, InvalidOperation, TypeError):
            logger.warning("Binance Futures ticker/24hr: пропущено %r — неповні дані", entry)

    return snapshot


def fetch_open_interest(session: requests.Session, symbol: str) -> Optional[Decimal]:
    """Per-symbol — немає bulk-ендпоінта для Open Interest на Binance
    Futures. Викликати ЛИШЕ для symbols, що вже пройшли дешевший Tier
    A-фільтр (fetch_market_snapshot), не для всього ринку одразу."""
    response = session.get(OPEN_INTEREST_URL, params={"symbol": symbol}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    try:
        return Decimal(payload["openInterest"])
    except (KeyError, InvalidOperation, TypeError):
        logger.warning("Binance Futures openInterest: неповна відповідь для %s: %r", symbol, payload)
        return None


def open_interest_records(
    oi_usd_by_symbol: dict[str, Decimal],
    fetched_at: Optional[datetime] = None,
) -> list[NormalizedRecord]:
    """Перетворює вже дістаний per-symbol OI (у $) у записи для
    `raw_observations` під metric_id `{symbol}_open_interest_usd` —
    тією самою назвою, що й OKX, бо величина та сама (OI в доларах).

    **Чому нормалізація тут, а не в скринінгу** (rule 1, CLAUDE.md):
    формат сирого запису — справа шару збору. `analysis/` лише передає
    сюди значення, які вже дістав через `fetch_open_interest()`, і
    записує результат — власного парсингу не має.

    **Чому це взагалі зберігається** (docs/decisions.md 2026-10-15):
    bulk-знімок Binance OI не містить (ендпоінта немає), а per-symbol
    OI скринінг дістає ЩОДНЯ для Tier A-виживших — і до 2026-10-15
    викидав після одного прогону. Тобто запити вже оплачені, а історії
    по них не накопичувалось ніколи. Покриття цього ряду НЕРІВНОМІРНЕ
    (лише символи, що проходили фільтр) — читач
    (`crypto_screening/history.py`) з цим рахується: бере лише біржі,
    присутні на обох краях вікна.
    """
    stamp = fetched_at or datetime.now(timezone.utc)
    records: list[NormalizedRecord] = []
    for symbol, oi_usd in oi_usd_by_symbol.items():
        if oi_usd is None:
            continue
        records.append(
            NormalizedRecord(
                source=BinanceFuturesAdapter.source,
                metric_id=f"{symbol.lower()}_open_interest_usd",
                value=oi_usd,
                observed_at=stamp.date(),
                fetched_at=stamp,
                revision=None,
                raw_payload={"open_interest_usd": str(oi_usd), "per_symbol": True},
            )
        )
    return records


def fetch_klines(session: requests.Session, symbol: str, limit: int = 30, interval: str = "1d") -> list[dict]:
    """Свічки (той самий ендпоінт-стиль, що спотовий
    `binance_adapter.py:fetch()`, тут — futures) — ЦІНА-ІСТОРІЯ для
    RSI/тренду/пампу (crypto_screening/, крок 4.5). На відміну від
    OI/funding/обсягу (які наша власна `raw_observations`-історія лише
    почала накопичувати 2026-09-27), ціна доступна за роки одразу з
    біржі — не блокується на нашому власному щоденному зборі. Не
    зберігається в raw_observations (свідомо — screen-скрипт рахує
    індикатори "на льоту", не зберігає ряд; сама точка-снапшот
    (`fetch_market_snapshot()`) зберігається, цього досить для аудиту).

    `interval` — Binance kline interval ("1d" за замовчуванням для
    первинного скану/LONG, "4h" для погодинного моніторингу вже
    активних кандидатів, `config.py:
    MONITORING_KLINE_INTERVAL` — рішення користувача, 2026-10-03:
    "денний ТФ підходить для глобальних висновків, моніторинг уже
    відібраних активів потрібно на меншому"). Стандартні значення
    Binance (`1m`/`5m`/`1h`/`4h`/`1d`/...) — caller відповідає за
    валідність.

    Повертає [{"value": float, "volume": float, "observed_at": date}, ...]
    у ХРОНОЛОГІЧНОМУ порядку (найстаріше перше) — той самий формат,
    що forecasting/trend.py очікує, і той самий порядок, що
    `backtest.py:backtest_metric()` приймає ПІСЛЯ розвороту (тут
    розворот не потрібен — Binance klines самі йдуть у хронологічному
    порядку, на відміну від fetch_recent()). `observed_at` — дата
    ВІДКРИТТЯ свічки навіть для `interval` коротшого за добу (лише
    `rsi()`/`backtest_metric()` використовують `value`, `observed_at`
    тут не критична точність для внутрішньодобових інтервалів).
    `volume` — обсяг САМЕ цієї свічки (не 24h rolling, на відміну від
    `fetch_market_snapshot()`) — для сплеску обсягу на короткому ТФ.

    Живо виявлено 2026-09-27 (crypto_screening/run_screening.py, повний
    прогін по 141 Tier A-виживших): символи, узгоджені через Bybit/OKX
    (aggregate_sources.py), не завжди торгуються на Binance Futures під
    тією самою назвою (напр. CASHCATUSDT, OKBUSDT) — Binance повертає
    HTTP 400 (invalid symbol), не порожній список. Це ОЧІКУВАНИЙ випадок
    (не помилка джерела), тому ловимо тут і повертаємо [] — той самий
    шлях, що "замало історії" (RSI/тренд/памп коректно стануть
    None/False/None, screen_long/screen_short_or_watch це вже
    трактують як "критерій не пройдено", не падають)."""
    try:
        response = session.get(
            KLINES_URL, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=30
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.warning("Binance Futures klines: %s недоступний (%s) — можливо, не торгується на Binance", symbol, e)
        return []
    raw = response.json()

    if not isinstance(raw, list):
        logger.warning("Binance Futures klines: неочікувана відповідь для %s: %r", symbol, raw)
        return []

    closes = []
    for row in raw:
        try:
            open_time_ms, close, volume = row[0], row[4], row[5]
            closes.append({
                "value": float(close),
                "volume": float(volume),
                "observed_at": datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).date(),
            })
        except (IndexError, TypeError, ValueError):
            logger.warning("Binance Futures klines: пропущено рядок для %s: %r", symbol, row)
    return closes


class BinanceFuturesAdapter(BaseAdapter):
    """Bulk-адаптер для raw_observations (rule 6 — сирі дані зберігаємо
    навіть для universe-скринінгу, не лише watchlist; funding rate/обсяг
    самі є вхідними рядами для скринінг-логіки, потрібна історія, не
    лише поточне значення). На відміну від BinanceAdapter (один symbol
    на екземпляр), тут ОДИН collect() охоплює ВЕСЬ eligible ринок
    одразу — bulk-джерело, форсувати "один symbol на виклик" означало б
    у N разів більше запитів замість 2 сумарних на весь ринок."""

    source = "binance_futures"

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()

    def fetch(self, **kwargs) -> Any:
        return fetch_market_snapshot(self.session)

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        observed_at = fetched_at.date()
        records: list[NormalizedRecord] = []

        for symbol, data in raw_response.items():
            metric_prefix = symbol.lower()
            records.append(
                NormalizedRecord(
                    source=self.source,
                    metric_id=f"{metric_prefix}_quote_volume",
                    value=data["quote_volume"],
                    observed_at=observed_at,
                    fetched_at=fetched_at,
                    revision=None,
                    raw_payload=data,
                )
            )
            records.append(
                NormalizedRecord(
                    source=self.source,
                    metric_id=f"{metric_prefix}_funding_rate",
                    value=data["funding_rate"],
                    observed_at=observed_at,
                    fetched_at=fetched_at,
                    revision=None,
                    raw_payload=data,
                )
            )

        return records
