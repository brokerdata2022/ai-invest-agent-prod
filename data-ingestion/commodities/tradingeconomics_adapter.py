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
from datetime import date, datetime, timezone
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
    # НЕ watchlist-актив, а факторний ряд (config.FACTOR_SERIES, 2026-10-20):
    # друге джерело для міді — проксі промислового попиту на срібло.
    # Живо 2026-10-20: "Copper rose to 6.56 USD/Lbs on October 20, 2026".
    "copper": "copper",
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


# ---------------------------------------------------------------------
# Сторінки-ІНДИКАТОРИ (не товари): тижнева зміна запасів нафти й газу
# ---------------------------------------------------------------------
#
# Навіщо (2026-10-20): друге, незалежне від EIA API джерело для звірки
# запасів (`config.FACTOR_SERIES`). TradingEconomics передруковує звіт
# EIA, тобто це пара "дзеркало ↔ першоджерело" — та сама, що FRED ↔ BLS
# для макро: ловить зіпсований/застряглий ряд на будь-якому боці.
#
# TE публікує не РІВЕНЬ, а ЗМІНУ за тиждень, тому звірка порівнює її з
# різницею двох останніх точок EIA (`monitoring/crosscheck_factors.py`).
# Значення тут — у тих самих одиницях, що EIA: нафта в ТИСЯЧАХ барелів
# (EIA `MBBL`), газ у bcf.
#
# Живий формат meta-опису (2026-10-20):
#   "Stocks of crude oil in the United States decreased by 8.56million
#    barrels in the week ending October 9 of 2026."
#   "Working gas held in storage facilities in the United States
#    increased by 67 billion cubic feet in the week ending October 9 of 2026 ."

TRADINGECONOMICS_INDICATOR_URL = "https://tradingeconomics.com/{path}"

# metric_id → (шлях сторінки, множник до одиниць EIA, очікувана одиниця в тексті)
INDICATOR_METRICS: dict[str, tuple[str, Decimal, str]] = {
    "us_crude_stocks_change": ("united-states/crude-oil-stocks-change", Decimal("1000"), "barrels"),
    "us_natgas_storage_change": ("united-states/natural-gas-stocks-change", Decimal("1"), "cubic feet"),
}

_UP_WORDS = ("increased", "rose", "grew", "climbed", "jumped", "added")
_DOWN_WORDS = ("decreased", "fell", "declined", "dropped", "slid", "drew")

_INDICATOR_RE = re.compile(
    r'name="description"\s+content="[^"]*?\b('
    + "|".join(_UP_WORDS + _DOWN_WORDS)
    + r")\s+by\s+([\d,]+\.?\d*)\s*(million|billion)?\s*([a-z ]+?)\s+in the week ending\s+"
    r"([A-Za-z]+ \d{1,2})\s+of\s+(\d{4})",
)


class TradingEconomicsIndicatorAdapter(BaseAdapter):
    source = "tradingeconomics"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in INDICATOR_METRICS:
            raise ValueError(
                f"Невідомий індикатор TradingEconomics: {metric_id!r}. "
                f"Доступні: {sorted(INDICATOR_METRICS)}"
            )
        self.metric_id = metric_id
        self.path, self.multiplier, self.expected_unit = INDICATOR_METRICS[metric_id]
        self.session = session or requests.Session()

    def fetch(self, **kwargs) -> Any:
        url = TRADINGECONOMICS_INDICATOR_URL.format(path=self.path)
        response = self.session.get(url, headers=_HEADERS, timeout=30)
        response.raise_for_status()
        return response.text

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        """На відміну від `TradingEconomicsAdapter.normalize` (порожній
        список при зміні формату), тут — виняток: ряд потрібен для
        ЗВІРКИ, і тихий порожній результат читався б як "звіряти нічим",
        тобто перевірка мовчки вимкнулась би назавжди."""
        fetched_at = datetime.now(timezone.utc)

        match = _INDICATOR_RE.search(raw_response) if isinstance(raw_response, str) else None
        if match is None:
            raise ValueError(
                f"TradingEconomics {self.metric_id} ({self.path}): не вдалось "
                f"розпарсити тижневу зміну з meta-опису — формат сторінки змінився?"
            )

        verb, raw_value, scale, unit_text, month_day, year = match.groups()
        if self.expected_unit not in unit_text:
            raise ValueError(
                f"TradingEconomics {self.metric_id}: одиниця {unit_text!r}, "
                f"очікувалась {self.expected_unit!r}"
            )

        value = Decimal(raw_value.replace(",", ""))
        # "8.56million barrels" → тисячі барелів (EIA MBBL); "67 billion
        # cubic feet" → bcf. Масштаб у тексті мусить відповідати одиниці
        # EIA, інакше множник хибний.
        if self.expected_unit == "barrels" and scale != "million":
            raise ValueError(f"TradingEconomics {self.metric_id}: масштаб {scale!r}, очікувався 'million'")
        if self.expected_unit == "cubic feet" and scale != "billion":
            raise ValueError(f"TradingEconomics {self.metric_id}: масштаб {scale!r}, очікувався 'billion'")
        value = value * self.multiplier
        if verb in _DOWN_WORDS:
            value = -value

        observed_at = datetime.strptime(f"{month_day} {year}", "%B %d %Y").date()

        return [
            NormalizedRecord(
                source=self.source,
                metric_id=self.metric_id,
                value=value,
                observed_at=observed_at,
                fetched_at=fetched_at,
                raw_payload={"path": self.path, "matched_text": match.group(0)[-160:]},
            )
        ]


# ---------------------------------------------------------------------
# Сторінки "значення за період ← попередній період" (2026-10-20)
# ---------------------------------------------------------------------
#
# Видобуток нафти Саудівської Аравії (щомісяця) — друге джерело для
# звірки з EIA STEO; золоті резерви центробанків (щокварталу, дані WGC/
# МВФ) — єдине безкоштовне джерело закупівель ЦБ.
#
# Живий формат (2026-10-20):
#   "Crude Oil Production in Saudi Arabia increased to 8372 BBL/D/1K in
#    September from 6238 BBL/D/1K in August of 2026."
#   "Gold Reserves in China increased to 2346.43 Tonnes in the second
#    quarter of 2026 from 2313.46 Tonnes in the first quarter of 2026."
#   "Gold Reserves in India remained unchanged at 880.52 Tonnes in the
#    second quarter of 2026 from 880.52 Tonnes in the first quarter of 2026."
#
# Зі сторінки беруться ОБИДВІ точки (поточна й попередня): так ряд має
# напрямок із першого ж прогону.

# metric_id → (шлях, множник до одиниць пари/конфіга, очікувана одиниця в тексті)
PERIOD_METRICS: dict[str, tuple[str, Decimal, str]] = {
    # BBL/D/1K = тисячі барелів на добу → млн бар./добу, як у EIA STEO
    "saudi_crude_production": ("saudi-arabia/crude-oil-production", Decimal("0.001"), "BBL/D/1K"),
    "china_gold_reserves": ("china/gold-reserves", Decimal("1"), "Tonnes"),
    "poland_gold_reserves": ("poland/gold-reserves", Decimal("1"), "Tonnes"),
}

_MONTHS = {name: index for index, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"), start=1)}
_QUARTER_END = {"first": (3, 31), "second": (6, 30), "third": (9, 30), "fourth": (12, 31)}

_PERIOD_RE = re.compile(
    r'name="description"\s+content="[^"]*?\b(?:increased|decreased|rose|fell|remained unchanged)\s+'
    r"(?:to|at)\s+([\d,]+\.?\d*)\s*(\S+)\s+in\s+(?:the\s+)?(.+?)\s+from\s+([\d,]+\.?\d*)\s*\S+\s+"
    r"in\s+(?:the\s+)?(.+?)\s*\.\s"
)


def _quarter_date(text: str) -> Optional[date]:
    match = re.fullmatch(r"(first|second|third|fourth) quarter of (\d{4})", text.strip())
    if not match:
        return None
    month, day = _QUARTER_END[match.group(1)]
    return date(int(match.group(2)), month, day)


def parse_period_pair(latest_text: str, previous_text: str, today: date) -> tuple[date, date]:
    """(дата поточної точки, дата попередньої). Квартал → його кінець;
    місяць → перше число.

    Для місяців TE пише рік лише в кінці ("in September from ... in
    August of 2026"), і на межі року незрозуміло, до якого місяця він
    належить. Тому з двох кандидатів (рік як є / рік+1) береться той,
    що не в майбутньому й найближчий до сьогодні."""
    latest_q, previous_q = _quarter_date(latest_text), _quarter_date(previous_text)
    if latest_q and previous_q:
        return latest_q, previous_q

    month_re = re.compile(r"([A-Z][a-z]+)(?: of (\d{4}))?")
    latest_m = month_re.fullmatch(latest_text.strip())
    previous_m = month_re.fullmatch(previous_text.strip())
    if not (latest_m and previous_m) or latest_m.group(1) not in _MONTHS or previous_m.group(1) not in _MONTHS:
        raise ValueError(f"не розпізнано періоди {latest_text!r} / {previous_text!r}")

    year = int(latest_m.group(2) or previous_m.group(2) or today.year)
    month = _MONTHS[latest_m.group(1)]
    candidates = [date(y, month, 1) for y in (year - 1, year, year + 1) if date(y, month, 1) <= today]
    if not candidates:
        raise ValueError(f"період {latest_text!r} у майбутньому")
    latest = max(candidates)
    previous_month = _MONTHS[previous_m.group(1)]
    previous_year = latest.year if previous_month < latest.month else latest.year - 1
    return latest, date(previous_year, previous_month, 1)


class TradingEconomicsPeriodAdapter(BaseAdapter):
    source = "tradingeconomics"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in PERIOD_METRICS:
            raise ValueError(f"Невідома сторінка TradingEconomics: {metric_id!r}. Доступні: {sorted(PERIOD_METRICS)}")
        self.metric_id = metric_id
        self.path, self.multiplier, self.expected_unit = PERIOD_METRICS[metric_id]
        self.session = session or requests.Session()

    def fetch(self, **kwargs) -> Any:
        response = self.session.get(TRADINGECONOMICS_INDICATOR_URL.format(path=self.path), headers=_HEADERS, timeout=30)
        response.raise_for_status()
        return response.text

    def normalize(self, raw_response: Any, today: Optional[date] = None) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        today = today or fetched_at.date()

        match = _PERIOD_RE.search(raw_response) if isinstance(raw_response, str) else None
        if match is None:
            raise ValueError(
                f"TradingEconomics {self.metric_id} ({self.path}): не вдалось "
                f"розпарсити значення з meta-опису — формат сторінки змінився?"
            )
        latest_raw, unit, latest_text, previous_raw, previous_text = match.groups()
        if unit != self.expected_unit:
            raise ValueError(f"TradingEconomics {self.metric_id}: одиниця {unit!r}, очікувалась {self.expected_unit!r}")

        latest_date, previous_date = parse_period_pair(latest_text, previous_text, today)
        records = []
        for raw_value, observed_at in ((previous_raw, previous_date), (latest_raw, latest_date)):
            records.append(NormalizedRecord(
                source=self.source,
                metric_id=self.metric_id,
                value=Decimal(raw_value.replace(",", "")) * self.multiplier,
                observed_at=observed_at,
                fetched_at=fetched_at,
                raw_payload={"path": self.path, "unit": unit, "matched_text": match.group(0)[-200:]},
            ))
        return records
