"""
Адаптер TradingEconomics — щоденна ціна товарних watchlist-активів, для
яких НЕМАЄ жодного структурованого API зі щоденною свіжістю (2026-10-04,
critical rule 7 CLAUDE.md, рішення користувача: "мені не потрібні фікси
на окремі дані, мені потрібний один працюючий код... основне джерело +
резерв + працювати тільки з свіжими даними").

**Чому не просто common/manual_observation.py:** той механізм (ручний
крос-чек, WebFetch у сесії Claude) розв'язав staleness РАЗОВО, але
вимагав людину/LLM-сесію на КОЖНЕ оновлення — живий урок цієї самої
сесії: coffee/wti_crude/brent_crude/natgas застрягли на 2026-10-02,
бо ніхто не прогнав оновлення вдруге. `_watchlist_prices`
(orchestration/jobs.py) не вміє сама "гуляти в інтернеті й звіряти
сайти" — WebFetch доступний лише Claude в розмові, не Python-процесу
за розкладом.

**Чому цей сайт підлягає скрапінгу (на відміну від Stooq, відкинутого
2026-09-20 саме через JS бот-захист):** живо перевірено (curl, без
JS/headless-браузера) — сторінка `/commodity/<slug>` віддає ціну+дату
ПРЯМО в `<meta name="description">` одним реченням, той самий шаблон
для ВСІХ перевірених товарів (coffee/crude-oil/brent-crude-oil/
natural-gas): "<Name> rose to|fell to <value> <unit> on <Month DD,
YYYY>, ...". Жодного JS-рендерингу чи антибот-челенджу не
спостережено — звичайний `requests.get()` з User-Agent віддає HTML
одразу. Неофіційне джерело (сайт не документує публічний API для
цього) — тому явне логування будь-якої зміни формату (rule у
data-ingestion/CLAUDE.md: "неофіційне... повинен явно обробляти й
логувати помилки парсингу"), не мовчазний збій.

METRICS — watchlist-товари, для яких УЖЕ підтверджено живим запитом
(2026-10-04), що TradingEconomics дає потрібний інструмент: metric_id
→ URL-слаг сторінки commodity. Новий товарний watchlist-актив ТУТ і
далі вимагає коду (той самий принцип, що FRED/Binance/CoinGecko
METRICS) — на відміну від Twelve Data/Binance spot
(telegram_commands.py:/watchlist_add), тут немає живого
символьного пошуку, слаг треба підбирати вручну.

**xauusd (золото) додано сюди з ІНШОЇ причини, ніж решта чотирьох**
(2026-10-04, живий фідбек користувача: синтез показав "+0.55%" для
золота за тиждень, коли реальний рух — зниження, tradingeconomics
"second consecutive weekly decline"): корінь — НЕ застарілість, а
НЕСТАБІЛЬНІ ревізії Twelve Data forex-стилю OTC-котирування: той самий
calendar-день (2026-09-28) живо підтверджено отримав ДВІ ревізії з
різницею 81 пункт (4196.13 → 4115.08) вже ПІСЛЯ закриття дня —
`v_observations_latest_revision` бере ОСТАННЮ (найбільш "продрейфовану"),
не обов'язково найточнішу. Звірка з investing.com (ф'ючерси, окрема
дата-точка на день) показала: жодна з двох ревізій Twelve Data не була
стабільно ближчою до реального закриття — тобто це не "виправна"
помилка парсингу, а властивість джерела (CFD/OTC-ціна без єдиного
офіційного сетлменту, на відміну від біржового ф'ючерсу). TradingEconomics
дає ОДНЕ число на день (так само нестабільний у сенсі "спот vs
ф'ючерс", але БЕЗ внутрішнього дрейфу ревізій заднім числом) —
прийнятний компроміс для watchlist-цілей (ціна↔новини, не точна
арбітражна торгівля).
"""

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

TRADINGECONOMICS_URL = "https://tradingeconomics.com/commodity/{slug}"

# Звичайний браузерний User-Agent -- сторінка віддає порожній/інший
# вміст без нього (живо перевірено).
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# internal metric_id (== watchlist_assets.metric_id) → URL-слаг
# TradingEconomics (watchlist_assets.ticker, 2026-10-04, живо
# підтверджено: coffee/wti_crude/brent_crude/natgas/xauusd).
METRICS: dict[str, str] = {
    "coffee": "coffee",
    "wti_crude": "crude-oil",
    "brent_crude": "brent-crude-oil",
    "natgas": "natural-gas",
    "xauusd": "gold",
}

# "Crude Oil fell to 91.11 USD/Bbl on October 2, 2026, down 1.90% ..."
_DESCRIPTION_RE = re.compile(
    r'name="description"\s+content="[^"]*?(?:rose to|fell to)\s+'
    r"([\d,]+\.?\d*)\s+(\S+)\s+on\s+([A-Za-z]+ \d{1,2}, \d{4})",
)


class TradingEconomicsAdapter(BaseAdapter):
    source = "tradingeconomics"

    def __init__(self, metric_id: str, slug: Optional[str] = None, session: Optional[requests.Session] = None):
        if slug is not None:
            self.slug = slug
        elif metric_id in METRICS:
            self.slug = METRICS[metric_id]
        else:
            raise ValueError(
                f"Невідомий metric_id для TradingEconomics: {metric_id!r}. "
                f"Доступні: {sorted(METRICS)} (або передайте slug= явно)"
            )
        self.metric_id = metric_id
        self.session = session or requests.Session()

    def fetch(self, **kwargs) -> Any:
        url = TRADINGECONOMICS_URL.format(slug=self.slug)
        response = self.session.get(url, headers=_HEADERS, timeout=30)
        response.raise_for_status()
        return response.text

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)

        match = _DESCRIPTION_RE.search(raw_response) if isinstance(raw_response, str) else None
        if match is None:
            # Неофіційне джерело змінило формат сторінки чи HTML
            # несподіваний — логуємо явно (rule, data-ingestion/CLAUDE.md),
            # не падаємо: watchlist/freshness-перевірка (common/freshness.py)
            # сама позначить актив застарілим, якщо це триватиме.
            logger.warning(
                "TradingEconomics %s (%s): не вдалось розпарсити ціну з meta-опису — формат сторінки змінився?",
                self.metric_id, self.slug,
            )
            return []

        raw_value, unit, raw_date = match.groups()
        try:
            value = Decimal(raw_value.replace(",", ""))
        except InvalidOperation:
            logger.warning(
                "TradingEconomics %s (%s): не вдалось розпарсити значення %r",
                self.metric_id, self.slug, raw_value,
            )
            return []

        try:
            observed_at = datetime.strptime(raw_date, "%B %d, %Y").date()
        except ValueError:
            logger.warning(
                "TradingEconomics %s (%s): не вдалось розпарсити дату %r",
                self.metric_id, self.slug, raw_date,
            )
            return []

        return [
            NormalizedRecord(
                source=self.source,
                metric_id=self.metric_id,
                value=value,
                observed_at=observed_at,
                fetched_at=fetched_at,
                revision=None,  # визначається шаром збереження, common/db.py
                raw_payload={"unit": unit, "matched_date_text": raw_date, "slug": self.slug},
            )
        ]
