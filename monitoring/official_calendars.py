"""
Офіційні календарі публікацій для єврозони та Японії — ДАТА-АНКЕР
там, де FRED `/release/dates` не працює (він покриває лише US).

**Чому це з'явилось (2026-10-13).** `economic_calendar.py`
(ForexFactory-фід) покриває лише ПОТОЧНИЙ тиждень, тому
`refresh_calendar.py` для не-US показників майже завжди писав "немає
збігу в economic_calendar у межах горизонту — пропущено (немає іншого
джерела дати)". Живий результат за весь час роботи агента: із 10
записів у `release_log` лише 2 були не-US (`eurozone_hicp`,
`eurozone_unemployment_rate`), а `eurozone_deposit_rate`,
`japan_cpi`, `japan_policy_rate` НЕ отримали дати ЖОДНОГО РАЗУ. Тобто
три показники з п'ятнадцяти ніколи не проходили конвеєр
реліз→прогноз→порівняння, і не через брак коду, а через брак дати.

**Розподіл ролей після цієї зміни** (той самий, що вже був для US):
офіційне джерело дає ДАТУ, ForexFactory ЗБАГАЧУЄ часом/impact/
forecast, якщо знайдеться збіг. Було навпаки — ForexFactory був
єдиним джерелом і дати, і часу.

**Усі чотири парсери перевірені живими запитами 2026-10-13, ДО
написання коду** (data-ingestion/CLAUDE.md: "для будь-якого джерела
без реєстрації — попросити живу перевірку одним запитом ДО написання
адаптера"; урок docs/decisions.md 2026-09-14/20).

⚠️ **Це HTML-скрейпінг офіційних сторінок, не API.** Структура може
змінитись без попередження. Тому:
- кожен парсер НЕЗАЛЕЖНИЙ: зламана сторінка ECB не повинна забирати
  з собою Японію (`fetch_all()` ловить винятки по джерелу);
- порожній результат піднімає помилку, а не тихо віддає `[]` — той
  самий урок, що мʼякий троттл GDELT, який місяць читався як "немає
  новин";
- ForexFactory лишається резервом: якщо офіційна сторінка зламалась,
  працює старий шлях.

**Чого тут НЕМА і чому:** `eurozone_unemployment_rate`. Це дані
Eurostat, не ECB — у статистичному календарі ECB їх немає (перевірено
живим пошуком по сторінці: жодної згадки unemployment/labour). Окремий
структурований календар Eurostat не знайдено, тому цей показник
свідомо лишається на ForexFactory. Чесне обмеження, не недогляд.
"""

import logging
import re
from datetime import date, datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Контактний User-Agent — та сама вимога, що в SEC EDGAR: офіційні
# сайти справедливо не люблять безликих ботів.
HEADERS = {"User-Agent": "ai-invest-agent/1.0 (+https://github.com/; contact in .env)"}
TIMEOUT = 30

ECB_STATSCAL_URL = "https://www.ecb.europa.eu/press/calendars/statscal/html/index.en.html"
ECB_COUNCIL_URL = "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"
BOJ_MPM_URL = "https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm"
JP_CPI_SCHEDULE_URL = "https://www.stat.go.jp/english/data/cpi/1582.html"
FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _text(fragment: str) -> str:
    """HTML-фрагмент → плаский текст одним рядком."""
    without_tags = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", without_tags.replace("&nbsp;", " ")).strip()


def _get(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text


def _month(token: str) -> Optional[int]:
    return _MONTHS.get(token.strip().rstrip(".").lower()[:4]) or _MONTHS.get(
        token.strip().rstrip(".").lower()[:3]
    )


def fetch_ecb_statscal() -> list[tuple[datetime, str]]:
    """Статистичний календар ECB → [(дата-час, опис)].

    Формат сторінки (перевірено живо): `<dt>DD/MM/YYYY HH:MM</dt>` +
    `<dd>опис (Dataset: КОД) Reference period: ...</dd>`. Час — CET,
    але ми його НЕ переводимо в UTC тут: `refresh_calendar` далі й так
    нормалізує через `default_scheduled_datetime`/фід, а подвійна
    конвертація — класичне джерело помилок на годину.

    Живий замір: 85 пар, зокрема 8 записів HICP на 4 місяці наперед
    (flash estimate о 15:00 і повний о 12:00)."""
    html = _get(ECB_STATSCAL_URL)
    pairs = re.findall(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", html, re.S)
    out: list[tuple[datetime, str]] = []
    for raw_dt, raw_dd in pairs:
        stamp = _text(raw_dt)
        match = re.match(r"(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", stamp)
        if not match:
            continue
        day, month, year, hour, minute = match.groups()
        out.append((
            datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0)),
            _text(raw_dd),
        ))
    if not out:
        raise ValueError(
            f"ECB statscal: жодного запису не розпізнано ({len(pairs)} пар dt/dd) — "
            "ймовірно змінилась структура сторінки"
        )
    return out


def fetch_ecb_policy_meetings() -> list[date]:
    """Дати рішень Ради ECB по ставці — ДЕНЬ 2 засідання.

    Чому саме день 2: засідання дводенне, рішення й прес-конференція —
    на другий день ("monetary policy meeting (Day 2), followed by
    press conference"). Прогноз ставки має цілитись у день РІШЕННЯ
    (рішення 2026-10-11: "ставка прогнозується на засідання, не на
    завтра").

    "non-monetary policy meeting" свідомо відкидається — там ставку не
    вирішують.

    Живий замір: 29/10/2026, 17/12/2026, 04/02/2027, 18/03/2027…"""
    html = _get(ECB_COUNCIL_URL)
    pairs = re.findall(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", html, re.S)
    out: list[date] = []
    for raw_dt, raw_dd in pairs:
        description = _text(raw_dd)
        if "monetary policy meeting" not in description.lower():
            continue
        if "non-monetary" in description.lower():
            continue
        if "day 2" not in description.lower():
            continue
        match = re.match(r"(\d{2})/(\d{2})/(\d{4})", _text(raw_dt))
        if match:
            day, month, year = match.groups()
            out.append(date(int(year), int(month), int(day)))
    if not out:
        raise ValueError(
            "ECB mgcgc: не знайдено жодного дня рішення (Day 2) — "
            "ймовірно змінилась структура сторінки"
        )
    return sorted(out)


def fetch_boj_policy_meetings(today: Optional[date] = None) -> list[date]:
    """Дати рішень BOJ по ставці — ДЕНЬ 2 засідання (та сама логіка,
    що для ECB: рішення й публікація — на другий день).

    Формат рядка: `Oct. 29 (Thurs.), 30 (Fri.)` — РІК у рядку
    відсутній. Він у заголовку таблиці (`<caption>Table : 2026</caption>`),
    і сторінка містить ДВА роки одразу.

    Перша версія (2026-10-13) вгадувала рік "поточний, інакше
    наступний" — і це дало неправильний результат одразу на живих
    даних: рядки 2027 року отримували поточний рік, тож у списку
    з'являлись і 2026-10-30 (правильно, день 2 жовтневого засідання),
    і 2026-10-29 (хибно — це день 2 ЖОВТНЕВОГО 2027 засідання).
    Тому рік тепер читається з `<caption>`, а не відновлюється."""
    today = today or date.today()
    html = _get(BOJ_MPM_URL)
    out: list[date] = []

    # Розбиваємо на таблиці, щоб кожна мала свій рік із caption.
    for table in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S):
        caption = re.search(r"<caption[^>]*>(.*?)</caption>", table, re.S)
        year_match = re.search(r"(20\d\d)", _text(caption.group(1))) if caption else None
        if year_match is None:
            continue
        year = int(year_match.group(1))

        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
            match = re.match(
                r"([A-Za-z]+\.?)\s+(\d{1,2})\s*\([^)]*\),\s*(\d{1,2})\s*\(",
                _text(row),
            )
            if not match:
                continue
            month_token, _day1, day2 = match.groups()
            month = _month(month_token)
            if month is None:
                continue
            try:
                out.append(date(year, month, int(day2)))
            except ValueError:
                # Невалідна комбінація (31 лютого тощо) означає, що
                # формат змінився — краще пропустити запис, ніж
                # зсунути дату засідання.
                logger.warning(
                    "BOJ MPM: невалідна дата %s-%s-%s у рядку %r",
                    year, month, day2, _text(row)[:60],
                )
    if not out:
        raise ValueError(
            "BOJ MPM: не розпізнано жодної дати — ймовірно змінилась "
            "структура сторінки (очікуються таблиці з <caption>Table : YYYY</caption>)"
        )
    return sorted(set(out))


def fetch_jp_cpi_releases(today: Optional[date] = None) -> list[date]:
    """Дати публікації НАЦІОНАЛЬНОГО CPI Японії (Statistics Bureau).

    Структура таблиці (перевірено живо по КЛІТИНКАХ, не по плаского
    тексту):
        ['Survey month', 'Date of release', 'Survey month', 'Date of release']
        ['December, 2025', 'January 23, 2026', 'January, 2026', 'January 30, 2026']
    Перші дві колонки — Японія, другі дві — тільки Токіо
    (preliminary). Нас цікавить колонка 1 (`Date of release` для
    Японії): саме національний показник дає e-Stat у `japan_cpi`.

    Перша версія (2026-10-13) розбирала ПЛАСКИЙ текст рядка й брала
    другу знайдену пару "місяць число", вважаючи першу референсним
    місяцем. На живих даних це дало зсув: референсний місяць
    ("January, 2026") під патерн не підпадає взагалі — там кома й
    чотиризначний рік, — тож "друга пара" виявлялась датою ТОКІО. У
    списку з'являлись і 2026-02-20 (правильно), і 2026-02-27 (Токіо).
    Тому тепер розбираємо клітинки за індексом, а не вгадуємо за
    порядком у тексті.

    Рік у більшості рядків відсутній (є лише там, де змінюється) —
    відновлюємо хронологічно: таблиця йде за зростанням, тож коли
    номер місяця стає меншим за попередній, рік збільшується."""
    today = today or date.today()
    html = _get(JP_CPI_SCHEDULE_URL)

    parsed: list[tuple[int, int, Optional[int]]] = []  # (місяць, день, явний рік)
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [_text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if len(cells) < 2:
            continue
        match = re.match(r"([A-Za-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?$", cells[1].strip())
        if not match:
            continue
        month_token, day, explicit_year = match.groups()
        month = _month(month_token)
        if month is None:
            continue
        parsed.append((month, int(day), int(explicit_year) if explicit_year else None))

    if not parsed:
        raise ValueError(
            "JP CPI schedule: не розпізнано жодної дати публікації — "
            "ймовірно змінилась структура таблиці (очікується колонка 1 "
            "'Date of release' для Японії)"
        )

    # Рік: явний там, де вказаний; далі — хронологічне продовження.
    out: list[date] = []
    year = next((y for _, _, y in parsed if y is not None), today.year)
    previous_month = None
    for month, day, explicit_year in parsed:
        if explicit_year is not None:
            year = explicit_year
        elif previous_month is not None and month < previous_month:
            year += 1
        previous_month = month
        try:
            out.append(date(year, month, day))
        except ValueError:
            logger.warning("JP CPI: невалідна дата %s-%s-%s — пропущено", year, month, day)
    return sorted(set(out))


def fetch_fomc_meetings(today: Optional[date] = None) -> list[date]:
    """Дати рішень FOMC — ДЕНЬ 2 засідання (та сама логіка, що ECB/BOJ).

    **Чому це тут, а не у FRED-календарі.** `fed_funds_rate` став
    календарним показником 2026-10-11 (рішення: "ставка прогнозується
    НА ЗАСІДАННЯ, не на завтра"), і дату тоді брали з
    ForexFactory-фіду — бо FRED `/release/dates` для серії H.15 дає
    ЩОДЕННІ дати публікації, а не дати засідань. Фід покриває лише
    поточний тиждень, тож на практиці дати не було: живий прогін
    13 жовтня — "fed_funds_rate: ні офіційного календаря, ні збігу в
    economic_calendar — пропущено".

    Формат сторінки (перевірено живо): панелі по роках
    (`<h4>2026 FOMC Meetings</h4>`), у кожній пари
    `fomc-meeting__month` + `fomc-meeting__date`, де дата — діапазон
    "27-28" (зірочка означає засідання з прес-конференцією й
    прогнозами). Беремо ДРУГЕ число: рішення оголошують у другий день.

    Міжмісячні засідання ("Apr/May" + "28-1") обробляються окремо:
    день 2 належить ДРУГОМУ місяцю діапазону."""
    today = today or date.today()
    html = _get(FOMC_CALENDAR_URL)
    out: list[date] = []

    # Заголовок містить анкор: `<h4><a id="42828">2026 FOMC Meetings</a></h4>`
    for year_match in re.finditer(r"<h4[^>]*>(?:<a[^>]*>)?\s*(20\d\d)\s+FOMC Meetings", html):
        year = int(year_match.group(1))
        # Блок року — до наступного заголовка року або кінця сторінки.
        next_match = re.search(r"<h4[^>]*>(?:<a[^>]*>)?\s*20\d\d\s+FOMC Meetings", html[year_match.end():])
        block = html[year_match.end():year_match.end() + (next_match.start() if next_match else len(html))]

        months = [_text(m) for m in re.findall(r'fomc-meeting__month[^"]*"[^>]*>(.*?)</div>', block, re.S)]
        days = [_text(d) for d in re.findall(r'fomc-meeting__date[^"]*"[^>]*>(.*?)</div>', block, re.S)]

        for month_token, day_token in zip(months, days):
            # "8-9*" → день 2 = 9; "28-1" у "Apr/May" → 1 травня.
            numbers = re.findall(r"\d{1,2}", day_token)
            if not numbers:
                continue
            day2 = int(numbers[-1])
            tokens = re.split(r"[/\s]+", month_token.strip())
            # Міжмісячне засідання: день 2 — у ДРУГОМУ місяці.
            month = _month(tokens[-1]) if len(tokens) > 1 else _month(tokens[0])
            if month is None:
                continue
            try:
                out.append(date(year, month, day2))
            except ValueError:
                logger.warning(
                    "FOMC: невалідна дата %s-%s-%s (%r / %r)",
                    year, month, day2, month_token, day_token,
                )
    if not out:
        raise ValueError(
            "FOMC calendar: не розпізнано жодної дати — ймовірно змінилась "
            "структура сторінки (очікуються fomc-meeting__month/__date)"
        )
    return sorted(set(out))


# metric_id → як дістати дати. Значення — функція без аргументів, що
# повертає список `date`. `eurozone_hicp` окремо, бо дату треба
# ВИБРАТИ з описів статистичного календаря, а не взяти список як є.
def ecb_hicp_releases() -> list[date]:
    """Дати публікації HICP єврозони зі статистичного календаря ECB.

    Беремо FLASH ESTIMATE, а не повний реліз: саме flash рухає ринок
    (виходить ~1-го числа, за два тижні до повного), і саме його
    прогнозує ForexFactory-консенсус, з яким ми потім порівнюємо
    факт. Повний реліз о 12:00 через два тижні — уточнення, не
    подія."""
    out = []
    for stamp, description in fetch_ecb_statscal():
        lowered = description.lower()
        if "hicp" in lowered and "flash estimate" in lowered:
            out.append(stamp.date())
    if not out:
        raise ValueError(
            "ECB statscal: не знайдено жодного HICP flash estimate — "
            "перевірити формулювання в описах календаря"
        )
    return sorted(set(out))


OFFICIAL_DATE_SOURCES = {
    "fed_funds_rate": fetch_fomc_meetings,
    "eurozone_hicp": ecb_hicp_releases,
    "eurozone_deposit_rate": fetch_ecb_policy_meetings,
    "japan_policy_rate": fetch_boj_policy_meetings,
    "japan_cpi": fetch_jp_cpi_releases,
}


def fetch_official_dates(metric_id: str) -> Optional[list[date]]:
    """Дати релізу показника з ОФІЦІЙНОГО джерела, або None якщо для
    цього показника офіційного джерела немає
    (`eurozone_unemployment_rate` — Eurostat, джерела не знайдено).

    Виняток парсера НЕ придушується: викликач
    (`refresh_calendar.py`) логує його по показнику й переходить до
    наступного, тож зламана сторінка одного джерела не зупиняє решту.
    Падати тут голосно важливіше, ніж віддати порожньо: тихий `[]`
    виглядав би як "релізів немає" — рівно та помилка, через яку
    мʼякий троттл GDELT місяць залишався непоміченим."""
    fetcher = OFFICIAL_DATE_SOURCES.get(metric_id)
    if fetcher is None:
        return None
    return fetcher()
