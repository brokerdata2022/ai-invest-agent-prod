"""
Yahoo Finance chart API — ДРУГЕ незалежне джерело ціни для
watchlist-активів (товари, форекс, крипта), з ІСТОРІЄЮ.

**Чому це з'явилось (2026-10-12, рішення користувача).** Формулювання
користувача після розбору живого виводу: "корекні дані це онлайн
пошук і звірка на декількох джерелах, а ці наші типу основні джерела
в них або застарілі дані або вони лягають через ліміти". Живі докази
того дня, обидва з БД:

1. **FRED помиляється на третину.** Brent за 2026-10-02: FRED
   135.51, TradingEconomics 102.25, а РУЧНА звірка по двох сайтах
   (`manual_observation.py`, source=web_crosscheck) — 102.25. Тобто
   першоджерело давало викид на 33%, і ніщо цього не ловило. WTI тієї
   ж дати: FRED 97.89 проти 91.11. Кава у FRED — узагалі з ЛИПНЯ
   (місячна серія `PCOFFOTMUSDM`).
2. **Історії немає.** TradingEconomics додано 2026-10-04 як фікс
   свіжості (critical rule 7), але історію ніхто не добрав: 6-7
   точок на актив. `config.TREND_MIN_POINTS` = 10, `TREND_LONG_DAYS`
   = 60 — тож тренд-шар торгового списку був СЛІПИЙ на 7 із 10
   активів і друкував "історії недосить для тренду (7 точок)".

**Живий замір ПЕРЕД написанням адаптера** (той самий урок, що в
docs/decisions.md 2026-09-14/20: "для джерела без реєстрації —
попросити живу перевірку одним запитом ДО написання коду"). 15
запитів із контейнера, усі 200:

| актив | Yahoo | TradingEconomics | FRED |
|---|---|---|---|
| Brent | 106.95 | 106.65 | 135.51 ❌ |
| WTI | 93.98 | 93.67 | 97.89 ❌ |
| Золото | 4194.50 | 4172.68 | — |
| Срібло | 60.56 | 60.77 (CoinGecko-проксі) | — |
| Газ | 3.14 | 3.14 ✓ | — |
| Кава | 289.90 | 285.89 | з липня ❌ |

Розбіжність Yahoo↔TradingEconomics — 0.3-1.4%, тобто два НЕЗАЛЕЖНІ
джерела узгоджені, а FRED — викид. Це і є звірка, якої просив
користувач, і вона одразу дала результат.

Глибина історії (`range=6mo`): **127-184 точки** на кожен актив.

**Чому це НЕ суперечить рішенню 2026-09-14/20** ("Stooq → Alpaca →
Stooq → Twelve Data"). Той розбір був про ціни **503 тикерів акцій**
для скринінгу, і там Twelve Data виграв по праву; Yahoo chart API у
тому порівнянні не розглядався взагалі. Відкидали тоді Stooq за
JS-бот-захист і Alpaca за гео-блок із України — ні того, ні того тут
немає (15 живих запитів пройшли). І головне: це не заміна Twelve Data,
а ДРУГЕ джерело для 11 watchlist-активів, де першоджерело
демонстровано бреше. Twelve Data лишається для акцій.

⚠️ **НЕОФІЦІЙНЕ джерело** (`data-ingestion/CLAUDE.md`): ключа немає,
документації немає, формат може змінитись без попередження. Тому
адаптер явно логує будь-яку несподівану структуру відповіді й падає
голосно, а не тихо віддає нуль записів. Якщо недоступне кілька днів
поспіль — це підстава піднімати пріоритет платного аналога, не
латати парсинг нескінченно (те саме правило, за яким викинуто Stooq).
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Yahoo відмовляє на запит без browser-like User-Agent — та сама
# поведінка, через яку відкинуто Stooq, але тут її достатньо обійти
# заголовком (не потрібен headless-браузер): перевірено живо.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ai-invest-agent/1.0)"}

# internal metric_id → символ Yahoo. Тільки watchlist-активи: акції
# йдуть через Twelve Data і сюди не переносяться.
#
# Символи перевірені живим запитом 2026-10-12, усі віддали 200 і
# осмислене значення. `JPY=X` — це саме USD/JPY (скільки єн за
# долар), збіглося з нашим 158.25 від Twelve Data.
SYMBOLS: dict[str, str] = {
    "brent_crude": "BZ=F",
    "wti_crude": "CL=F",
    "xauusd": "GC=F",
    "xagusd": "SI=F",
    "natgas": "NG=F",
    "coffee": "KC=F",
    "eurusd": "EURUSD=X",
    "usdjpy": "JPY=X",
    "btc": "BTC-USD",
    "eth": "ETH-USD",
    "sol": "SOL-USD",
}


# Скільки денних точок приблизно дає кожен період Yahoo (торгові дні,
# не календарні — тому ~21 на місяць). Упорядковано за зростанням:
# `_range_for` бере ПЕРШИЙ, що покриває потрібну кількість.
_RANGE_POINTS: tuple[tuple[str, int], ...] = (
    ("5d", 5),
    ("1mo", 21),
    ("3mo", 63),
    ("6mo", 127),
    ("1y", 252),
    ("2y", 504),
    ("5y", 1260),
    ("max", 10**9),
)

DEFAULT_RANGE = "6mo"


def _range_for(limit: Optional[int]) -> str:
    """`limit` точок → найменший період Yahoo, що їх покриває."""
    if limit is None:
        return DEFAULT_RANGE
    for name, points in _RANGE_POINTS:
        if points >= limit:
            return name
    return "max"


class YahooChartAdapter(BaseAdapter):
    source = "yahoo_chart"

    def __init__(self, metric_id: str, session: Optional[requests.Session] = None):
        if metric_id not in SYMBOLS:
            raise ValueError(
                f"Немає символу Yahoo для metric_id {metric_id!r}. "
                f"Доступні: {sorted(SYMBOLS)}"
            )
        self.metric_id = metric_id
        self.symbol = SYMBOLS[metric_id]
        self._session = session or requests.Session()

    def fetch(
        self,
        limit: Optional[int] = None,
        range_: Optional[str] = None,
        interval: str = "1d",
    ) -> Any:
        """`limit` — скільки останніх ДЕННИХ точок потрібно; це той
        самий kwarg, яким `run_collect.py` викликає ВСІ адаптери
        проєкту (єдиний інтерфейс, data-ingestion/CLAUDE.md), тому він
        тут головний, а `range_` — ручне перекриття для рідких
        випадків.

        Yahoo приймає не кількість точок, а період (`range`), тож
        `limit` переводиться у найменший період, що його покриває
        (`_range_for`). Зайві точки не шкодять: запис append-only з
        дедупом (critical rule 6), повторний збір тієї самої дати
        нічого не ламає.

        Дефолт 6mo (~127 денних точок) — із запасом покриває
        `config.TREND_LONG_DAYS` (60) і знімає рівно ту проблему,
        через яку тренд-шар торгового списку був сліпий на 7 із 10
        активів."""
        effective_range = range_ or _range_for(limit)
        response = self._session.get(
            CHART_URL.format(symbol=self.symbol),
            params={"range": effective_range, "interval": interval},
            headers=HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)

        try:
            result = raw_response["chart"]["result"][0]
            timestamps = result["timestamp"]
            closes = result["indicators"]["quote"][0]["close"]
        except (KeyError, IndexError, TypeError) as exc:
            # Неофіційне джерело: структура могла змінитись. Падаємо
            # голосно з назвою активу — тихий нуль записів тут гірший
            # за помилку (той самий урок, що мʼякий троттл GDELT,
            # який до 2026-10-05 читався як "немає новин").
            raise ValueError(
                f"Yahoo chart: несподівана структура відповіді для "
                f"{self.metric_id} ({self.symbol}): {exc}"
            ) from exc

        if len(timestamps) != len(closes):
            raise ValueError(
                f"Yahoo chart: {self.metric_id} — timestamp і close різної "
                f"довжини ({len(timestamps)} проти {len(closes)})"
            )

        records: list[NormalizedRecord] = []
        skipped = 0
        for ts, close in zip(timestamps, closes):
            # null у close — нормально: Yahoo віддає свічки для днів,
            # коли інструмент не торгувався (свята різних бірж).
            # Пропускаємо, а не пишемо нуль.
            if close is None:
                skipped += 1
                continue
            try:
                value = Decimal(str(close))
            except (InvalidOperation, ValueError):
                skipped += 1
                continue
            records.append(
                NormalizedRecord(
                    source=self.source,
                    metric_id=self.metric_id,
                    value=value,
                    observed_at=datetime.fromtimestamp(ts, tz=timezone.utc).date(),
                    fetched_at=fetched_at,
                    raw_payload={"symbol": self.symbol, "close": close},
                )
            )

        if skipped:
            logger.info(
                "%s (%s): пропущено %d точок без ціни (неторгові дні)",
                self.metric_id, self.symbol, skipped,
            )
        if not records:
            raise ValueError(
                f"Yahoo chart: {self.metric_id} ({self.symbol}) — жодної "
                f"придатної точки у відповіді"
            )
        return records


def latest_close(metric_id: str, session: Optional[requests.Session] = None) -> tuple[date, Decimal]:
    """Остання денна ціна одним викликом — для звірки джерел
    (`common/consensus.py`), коли історія не потрібна."""
    records = YahooChartAdapter(metric_id, session=session).collect(limit=5)
    newest = max(records, key=lambda r: r.observed_at)
    return newest.observed_at, newest.value


def latest_close_for_symbol(
    symbol: str, session: Optional[requests.Session] = None
) -> Optional[tuple]:
    """(дата, ціна) за САМИМ символом Yahoo, без прив'язки до `SYMBOLS`.

    Потрібна для звірки КОТИРУВАНЬ АКЦІЙ (`monitoring/crosscheck_quotes.py`,
    2026-10-15): `SYMBOLS` — це 11 watchlist-активів, а звіряти треба
    тикери S&P, яких у цій мапі немає й не має бути (їх 500+, і вони
    приходять зі скринінгу, не з конфіга).

    `None` замість винятку: звірка 30 тикерів не повинна падати цілком
    через один делістингований символ — відсутнє значення просто не
    потрапить у вердикт (`consensus.py` сам скаже "звірити ні з чим").
    """
    try:
        response = (session or requests.Session()).get(
            CHART_URL.format(symbol=symbol),
            params={"range": "5d", "interval": "1d"},
            headers=HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except Exception:
        logger.warning("Yahoo chart: %s не вдалось отримати", symbol, exc_info=True)
        return None

    for ts, close in sorted(zip(timestamps, closes), key=lambda pair: pair[0], reverse=True):
        if close is None:
            continue
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).date(), Decimal(str(close))
        except (InvalidOperation, ValueError):
            continue
    return None


def closes_by_date(
    symbol: str, session: Optional[requests.Session] = None, range_: str = "1mo"
) -> dict:
    """{дата: ціна закриття} за САМИМ символом Yahoo.

    Чому мапа, а не «остання ціна» (живий дефект 2026-10-15): перший
    варіант звірки котирувань порівнював НАШЕ значення за 09.10 із
    yahoo-значенням за 15.10 і давав розкид 2-5% — тобто міряв
    ЗАСТАРІЛІСТЬ нашого зрізу, а не незгоду джерел. Це та сама помилка,
    через яку звірка BLS спершу «знайшла» розбіжність 0.25%: треба
    брати ТОЙ САМИЙ ряд і ТУ САМУ дату (CLAUDE.md).

    Порожній дікт замість винятку — звірка 30 тикерів не має падати
    через один делістингований символ."""
    try:
        response = (session or requests.Session()).get(
            CHART_URL.format(symbol=symbol),
            params={"range": range_, "interval": "1d"},
            headers=HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except Exception:
        logger.warning("Yahoo chart: %s не вдалось отримати", symbol, exc_info=True)
        return {}

    series: dict = {}
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        try:
            series[datetime.fromtimestamp(ts, tz=timezone.utc).date()] = Decimal(str(close))
        except (InvalidOperation, ValueError):
            continue
    return series
