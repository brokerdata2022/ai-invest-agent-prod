"""
Адаптер Twelve Data — щоденні ціна/обсяг для довільного тикера.
https://twelvedata.com/docs

Третій кандидат на джерело котирувань у цьому проєкті (docs/decisions.md,
2026-09-14/20): Alpaca Markets відкинуто (недоступна з мережі
користувача), Stooq відкинуто (з ~вересня 2026 ставить JS-based
бот-перевірку на CSV-ендпоінт — недоступна для requests, обхід
означав би headless-браузер, крихка залежність для регулярного
збору). Twelve Data — перше з трьох, що підтверджено ЖИВИМ запитом
користувача (200, реальні дані AAPL) до того, як писався код, а не
після.

Безкоштовна реєстрація email+пароль (без капчі), ключ одразу в
кабінеті. Ліміти free tier: ~8 запитів/хв (після підтвердження
email), 800/добу — суттєво для прогону скринера на весь S&P 500
(~500 тикерів): по 1 запиту (1 credit) на тикер, тобто прогін
займає ~500/8 ≈ 63 хв, вкладається в добову квоту з запасом.

Архітектурна відмінність від macro/*-адаптерів (FRED/ECB/BOJ/e-Stat):
там METRICS — фіксований словник конкретних показників країни. Тут
тикер — необмежена множина (universe скринінгу — S&P 500, див.
config.py), тож METRICS-у стилі macro/ не буде:
тикер — параметр конструктора, а не хардкод. FIELDS нижче — це
фіксований (малий) набір **полів одного бару**, не показників.
metric_id збирається динамічно як "{ticker}_{field}", напр.
"aapl_close", "aapl_volume" — той самий підхід, що вже задокументовано
для companies/ (SEC EDGAR) у docs/decisions.md.

Також джерело цін для watchlist-металів (docs/decisions.md, ціль
1): Twelve Data підтверджено підтримує форекс/commodity-символи зі
слешем (напр. "XAU/USD", "XAG/USD" — twelvedata.com/docs, перевірено
2026-09-26; FRED видалив LBMA gold/silver fixing у 2022, докладніше
docs/decisions.md). Тикер зі слешем нормалізується (слеш прибирається)
перед побудовою metric_id, щоб "XAU/USD" дав metric_id "xauusd_close",
не "xau/usd_close" — це узгоджує його з внутрішнім asset_id
("xauusd", news/queries.py:WATCHLIST_ASSET_IDS), а не з форматом
символу конкретного джерела (rule "metric_id — свій внутрішній,
стабільний", CLAUDE.md).
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

TWELVEDATA_URL = "https://api.twelvedata.com/time_series"

# metric_id-суфікс → ключ у JSON "values"-записі Twelve Data. Для
# скринінгу (config.py) потрібні тільки ці два поля —
# open/high/low навмисно не тягнемо, поки для них немає використання.
FIELDS: dict[str, str] = {
    "close": "close",
    "volume": "volume",
}


SYMBOL_SEARCH_URL = "https://api.twelvedata.com/symbol_search"


def search_symbol(query: str, api_key: str, session: Optional[requests.Session] = None) -> Optional[str]:
    """Twelve Data symbol_search — довідниковий пошук, приймає
    ПРИБЛИЗНУ назву чи неточний формат тикера (напр. "NATGAS" замість
    точного каталожного символу) і повертає symbol САМЕ так, як його
    називає Twelve Data, або None, якщо нічого не знайдено чи запит
    провалився.

    Додано 2026-10-03 (docs/decisions.md, orchestration/
    telegram_commands.py:/watchlist_add) — живий кейс "NATGAS": точний
    рядок дав 404 (time_series), хоча інструмент, можливо, є в
    каталозі під іншим кодом — без symbol_search користувач мусив би
    вгадувати формат навмання. Перший результат пошуку беремо як
    найкращий збіг (Twelve Data сам сортує за релевантністю) —
    викликач (validate_symbol у тому самому модулі чи інший) МАЄ ще
    підтвердити, що символ реально має дані, перш ніж довіряти йому."""
    match = lookup_instrument(query, api_key, session=session)
    if match is None:
        return None
    logger.info("Twelve Data symbol_search %r → %r (%s)", query, match["symbol"], match["instrument_name"])
    return match["symbol"]


def lookup_instrument(
    query: str, api_key: Optional[str] = None, session: Optional[requests.Session] = None,
) -> Optional[dict]:
    """Перший збіг `symbol_search` як довідка про інструмент:
    `{symbol, instrument_name, instrument_type, exchange, country}`,
    або None.

    Додано 2026-10-22 (живий кейс SKYD — акцію показано "товаром"):
    тип інструмента в Twelve Data вже Є, і саме з нього `/watchlist_add`
    тепер визначає клас активу (`common/asset_profile.py:classify_instrument`).
    Живо: SKYD → "Skydance Corporation Class B Common Stock" / "Common
    Stock" / NYSE; XAU/USD → "Precious Metal" / COMMODITY; GBP/USD →
    "Physical Currency". `symbol_search` працює й без ключа — не
    витрачає кредитів.

    Точний збіг символу має пріоритет над першим рядком: пошук "NG"
    дає першою National Grid (LSE), і без цього правила збіг за
    назвою переміг би той символ, який користувач набрав дослівно."""
    session = session or requests.Session()
    params = {"symbol": query}
    if api_key:
        params["apikey"] = api_key
    try:
        response = session.get(SYMBOL_SEARCH_URL, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
    except (requests.exceptions.RequestException, ValueError):
        logger.warning("Twelve Data symbol_search провалився для %r", query, exc_info=True)
        return None

    matches = data.get("data") if isinstance(data, dict) else None
    if not matches:
        return None
    wanted = query.strip().upper()
    exact = [m for m in matches if str(m.get("symbol", "")).upper() == wanted]
    # Серед точних — спершу США: той самий тикер на BMV/LSE часто інший
    # інструмент або дубль без звітності SEC.
    exact.sort(key=lambda m: m.get("country") != "United States")
    best = (exact or matches)[0]
    return {
        "symbol": best.get("symbol"),
        "instrument_name": best.get("instrument_name"),
        "instrument_type": best.get("instrument_type"),
        "exchange": best.get("exchange"),
        "country": best.get("country"),
    }


class TwelveDataAdapter(BaseAdapter):
    source = "twelvedata"

    def __init__(
        self, api_key: str, ticker: str, metric_id: Optional[str] = None, session: Optional[requests.Session] = None,
    ):
        if not api_key:
            raise ValueError(
                "TWELVEDATA_API_KEY не задано. Безкоштовна реєстрація: "
                "https://twelvedata.com/ (див. .env.example)."
            )
        if not ticker or not ticker.strip():
            raise ValueError(f"Некоректний тикер для Twelve Data: {ticker!r}")
        self.api_key = api_key
        self.ticker = ticker.strip().upper()
        # metric_id override (2026-10-04, той самий принцип, що
        # crypto/binance_adapter.py:metric_id= для watchlist-активів,
        # доданих через Telegram): None (дефолт, усі існуючі виклики —
        # screening/discovery/run_collect.py) лишає стару
        # поведінку — normalize() сам виводить базу з ticker. Явний
        # override потрібен ЛИШЕ orchestration/jobs.py:_watchlist_prices
        # — коли /watchlist_add резолвнув тикер через symbol_search
        # (напр. "NATGAS" -> "NG"), DB-рядок watchlist_assets.metric_id
        # лишається "natgas_close" (на asset_id, не на резолвлений
        # тикер), а виведена з ticker база дала б "ng_close" — розбіжність
        # мовчки губила б усі зібрані ціни з жодної подальшої функції,
        # що читає watchlist_assets.metric_id (prices.py/watchlist_notify.py/
        # synthesize.py), ніколи не падаючи явно.
        self.metric_base = metric_id if metric_id is not None else self.ticker.lower().replace("/", "")
        self.session = session or requests.Session()

    def fetch(
        self,
        limit: Optional[int] = None,
        observation_start: Optional[str] = None,
        observation_end: Optional[str] = None,
    ) -> Any:
        params: dict[str, Any] = {
            "symbol": self.ticker,
            "interval": "1day",
            "apikey": self.api_key,
        }
        if limit:
            params["outputsize"] = limit
        if observation_start:
            params["start_date"] = observation_start
        if observation_end:
            params["end_date"] = observation_end

        response = self.session.get(TWELVEDATA_URL, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        records: list[NormalizedRecord] = []

        # Twelve Data повертає помилки зі status=200 (не HTTP-кодом
        # помилки) — {"code": 400/401/429/..., "status": "error",
        # "message": "..."} — тому raise_for_status() у fetch() цього
        # не ловить, перевіряємо явно тут.
        if isinstance(raw_response, dict) and raw_response.get("status") == "error":
            raise ValueError(
                f"Twelve Data повернув помилку для {self.ticker}: "
                f"код {raw_response.get('code')}, {raw_response.get('message')!r}"
            )

        values = raw_response.get("values") if isinstance(raw_response, dict) else None
        if not values:
            raise ValueError(
                f"Неочікувана відповідь Twelve Data для {self.ticker} — "
                f"немає values: {raw_response!r}"[:300]
            )

        for row in values:
            raw_date = row.get("datetime")
            if not raw_date:
                continue
            try:
                observed_at = date.fromisoformat(raw_date)
            except ValueError:
                logger.warning(
                    "Twelve Data %s: не вдалось розпарсити дату %r, рядок пропущено",
                    self.ticker, raw_date,
                )
                continue

            for suffix, key in FIELDS.items():
                raw_value = row.get(key)
                if raw_value in (None, ""):
                    logger.debug(
                        "Twelve Data %s: відсутнє значення %s за %s",
                        self.ticker, key, raw_date,
                    )
                    continue
                try:
                    value = Decimal(raw_value)
                except InvalidOperation:
                    logger.warning(
                        "Twelve Data %s: не вдалось розпарсити %s за %s: %r",
                        self.ticker, key, raw_date, raw_value,
                    )
                    continue

                records.append(
                    NormalizedRecord(
                        source=self.source,
                        metric_id=f"{self.metric_base}_{suffix}",
                        value=value,
                        observed_at=observed_at,
                        fetched_at=fetched_at,
                        revision=None,  # визначається шаром збереження, див. common/db.py
                        raw_payload=dict(row),
                    )
                )

        return records
