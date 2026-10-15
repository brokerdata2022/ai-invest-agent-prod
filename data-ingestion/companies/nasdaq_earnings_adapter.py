"""
Календар звітності компаній — Nasdaq earnings calendar.

**Навіщо (2026-10-15, рішення користувача).** Дат звітності в агента не
було взагалі: EPS брався як ЦИФРА в Tier B скринінгу
(`EarningsPerShareDiluted YoY`), але «коли вийде наступний звіт» ніхто
не знав. Релізний календар покриває лише 15 макропоказників, тому
прицільну подію акція мала тільки за наявності новини — зміряно
2026-10-13: власну подію мали 9 кандидатів із 29, решта стояла на
спільному макрофоні. Звіт — це каталізатор, розкладений наперед для
КОЖНОЇ акції.

**Чому Nasdaq, а не Twelve Data.** Живі запити 2026-10-15:
`twelvedata.com/earnings_calendar` → **403** («available exclusively
with grow or pro or ultra plans»), `twelvedata.com/earnings?symbol=AAPL`
→ 200, але ЛИШЕ МИНУЛІ звіти (найновіший 2026-07-30 при сьогоднішньому
15.10) — для каталізатора потрібна МАЙБУТНЯ дата. Yahoo
`quoteSummary?modules=calendarEvents` → **429**. Nasdaq віддає
майбутні дати з прогнозом EPS і часом (до/після сесії).

**Чому це НЕ першоджерело й чому так можна.** Той самий клас, що
ForexFactory для макрокалендаря (docs/decisions.md 2026-09-27):
неофіційний публічний endpoint без ключа, єдиний безкоштовний, що дає
потрібне. Першоджерело (сама компанія/SEC) майбутню дату взагалі не
публікує у машинному вигляді — 8-K з анонсом виходить за кілька днів
до звіту, тобто пізніше, ніж потрібно каталізатору.

**Що робить цей адаптер і чого НЕ робить.** Нормалізує рядки календаря
(rule 1 — лише збір, жодної інтерпретації). Не пише в
`raw_observations`: дата звіту — це не спостереження часового ряду, а
подія календаря, і живе вона в `earnings_calendar` (той самий поділ,
що `release_log` для макро).
"""

import logging
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

import requests

logger = logging.getLogger(__name__)

SOURCE = "nasdaq_earnings"
CALENDAR_URL = "https://api.nasdaq.com/api/calendar/earnings"

# Nasdaq віддає 403 на запит без браузерного User-Agent (перевірено
# живо). Той самий підхід, що в решті неофіційних джерел проєкту.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# "time-pre-market" / "time-after-hours" → людською мовою.
_TIME_LABELS = {
    "time-pre-market": "до відкриття",
    "time-after-hours": "після закриття",
    "time-not-supplied": None,
}


def _parse_money(raw: Optional[str]) -> Optional[Decimal]:
    """"$2.01" → Decimal("2.01"); "($0.15)" → Decimal("-0.15").

    Дужки — бухгортський мінус, і це не дрібниця: прогноз збитку,
    прочитаний як прибуток, перевернув би знак сюрпризу."""
    if not raw:
        return None
    text = raw.strip()
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        logger.warning("Nasdaq earnings: не розібрав значення %r", raw)
        return None
    return -value if negative else value


def fetch_day(session: requests.Session, day: date) -> list[dict]:
    """Сирі рядки календаря на одну дату. Порожній список — нормальний
    стан (вихідні, свята), не помилка."""
    response = session.get(
        CALENDAR_URL, params={"date": day.isoformat()}, headers=HEADERS, timeout=30
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    return data.get("rows") or []


def normalize(rows: list[dict], report_date: date) -> list[dict]:
    """Рядки Nasdaq → записи для `earnings_calendar`."""
    records: list[dict] = []
    for row in rows:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        records.append({
            "ticker": symbol,
            "report_date": report_date,
            "report_time": _TIME_LABELS.get(row.get("time"), row.get("time")),
            "eps_forecast": _parse_money(row.get("epsForecast")),
            "company_name": (row.get("name") or "").strip() or None,
            "source": SOURCE,
        })
    return records


def collect(
    horizon_days: int,
    session: Optional[requests.Session] = None,
    start: Optional[date] = None,
) -> list[dict]:
    """Календар на `horizon_days` днів уперед.

    Вихідні пропускаються без запиту: звітність виходить у робочі дні,
    і два зайві запити на тиждень нічого не додають. Збій одного дня не
    забирає решту — той самий принцип, що в мультибіржових джобах."""
    session = session or requests.Session()
    first = start or date.today()
    records: list[dict] = []

    for offset in range(horizon_days + 1):
        day = first + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        try:
            rows = fetch_day(session, day)
        except Exception:
            logger.warning("Nasdaq earnings: день %s не вдалось отримати", day, exc_info=True)
            continue
        records.extend(normalize(rows, day))

    return records
