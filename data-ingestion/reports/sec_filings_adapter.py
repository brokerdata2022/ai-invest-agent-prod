"""
Текст звітів 10-K/10-Q із SEC EDGAR — остання невиконана обіцянка
опису агента ("збирає ... звіти компаній").

**Чому це довго не існувало.** `companies/sec_edgar_adapter.py`
збирає з SEC лише ЧИСЛОВІ факти XBRL (revenue/EPS/акції/активи/
зобов'язання) — це інший endpoint (`companyconcept`) і інший тип
даних. Текст самого звіту — MD&A («як керівництво ПОЯСНЮЄ цифри»),
Risk Factors — не збирався ніколи, тому `data-ingestion/reports/` у
структурі репозиторію стояла з позначкою "ще не існує".

Чому це важливо саме для нашого агента: числа кажуть ЩО сталося,
MD&A каже ЧОМУ, і це єдине місце, де компанія сама називає причини.
Фундаментальний аналіз 12 жовтня впирався рівно в це — він переказував
квартальні цифри ("виручка зросла з 3.69 до 5.04 млрд") і не міг
сказати, чому; у звіті це написано словами.

**Живий замір ПЕРЕД кодом** (data-ingestion/CLAUDE.md). AAPL,
CIK 320193:
- `submissions/CIK##########.json` → список форм із
  `accessionNumber`/`primaryDocument`; останні 10-Q: 2026-07-31,
  2026-05-01, 2026-01-30; останній 10-K: 2025-10-31;
- сам документ (`Archives/edgar/data/<cik>/<acc>/<doc>`) → HTTP 200,
  994 КБ HTML, після зняття розмітки **92 КБ плаского тексту**.

**Пастка розбору, яку видно лише на живому документі.** Заголовки
розділів ("Management's Discussion and Analysis", "Risk Factors")
зустрічаються в документі ПО КІЛЬКА РАЗІВ, і ПЕРШЕ входження — це
ЗМІСТ (table of contents), а не сам розділ: у живому 10-Q "discussion
and analysis" знайдено на позиціях 15080, 41995, 62787, і перша — це
рядок змісту "…Results of Operations 13 Item 3…". Тому розділ
вибирається не першим входженням, а тим, після якого найбільше тексту
до наступного маркера: тіло розділу довше за рядок змісту на порядки.

⚠️ Обсяг: один документ ~1 МБ HTML. Тому ліміт форм на тикер і
пауза між запитами (SEC fair-access: 10 запитів/с, але ми й близько
не підходимо — це джоба на кілька тикерів раз на тиждень).
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
DOCUMENT_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

WANTED_FORMS = ("10-K", "10-Q")
_SECONDS_BETWEEN_REQUESTS = 0.2

# Розділи, які справді несуть зміст для аналізу. Ключ — наша назва,
# значення — (патерн початку, патерни можливого кінця).
#
# `risk_factors` у 10-Q зазвичай короткий ("змін не було порівняно з
# 10-K") — це нормально й саме по собі інформація.
SECTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "mdna": (
        r"discussion\s+and\s+analysis",
        (r"quantitative\s+and\s+qualitative", r"item\s+4", r"controls\s+and\s+procedures"),
    ),
    "risk_factors": (
        r"risk\s+factors",
        (r"unregistered\s+sales", r"item\s+2\b", r"legal\s+proceedings"),
    ),
}

# Нижче цього розділ вважаємо не знайденим: у 10-Q справжній MD&A — це
# десятки кілобайт, а кілька сотень символів означають, що ми
# зачепили рядок змісту або перехресне посилання.
MIN_SECTION_CHARS = 1500


@dataclass
class Filing:
    ticker: str
    cik: int
    form: str
    filing_date: date
    accession: str
    document: str
    url: str
    full_text: str
    sections: dict[str, str]
    fetched_at: datetime


def html_to_text(html: str) -> str:
    """HTML звіту → плаский текст одним рядком.

    `<script>`/`<style>` вирізаються ПЕРЕД зняттям тегів: інакше їхній
    вміст потрапляє в текст і засмічує його (у звітах SEC це переважно
    inline-XBRL-службовий код)."""
    without_code = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    without_tags = re.sub(r"<[^>]+>", " ", without_code)
    decoded = re.sub(r"&nbsp;|&#160;", " ", without_tags)
    decoded = re.sub(r"&amp;", "&", decoded)
    decoded = re.sub(r"&[a-zA-Z]+;|&#\d+;", " ", decoded)
    return re.sub(r"\s+", " ", decoded).strip()


def extract_section(text: str, start_pattern: str, end_patterns: tuple[str, ...]) -> Optional[str]:
    """Тіло розділу за патерном заголовка.

    Вибирає НЕ перше входження, а те, після якого найбільше тексту до
    найближчого кінцевого маркера — бо перше входження зазвичай рядок
    ЗМІСТУ (живий приклад у докстрінгу модуля). Повертає None, якщо
    найдовший кандидат усе одно коротший за `MIN_SECTION_CHARS`."""
    starts = [m.end() for m in re.finditer(start_pattern, text, re.I)]
    if not starts:
        return None

    best: Optional[str] = None
    for start in starts:
        ends = [
            m.start()
            for pattern in end_patterns
            for m in re.finditer(pattern, text[start:], re.I)
        ]
        stop = start + min(ends) if ends else len(text)
        candidate = text[start:stop].strip()
        if best is None or len(candidate) > len(best):
            best = candidate

    if best is None or len(best) < MIN_SECTION_CHARS:
        return None
    return best


class SecFilingsAdapter:
    """НЕ `BaseAdapter`: той інтерфейс розрахований на числові
    `NormalizedRecord` (`value`/`observed_at`), а тут результат —
    ТЕКСТ. Той самий привід, через який `news/` має власний
    паралельний інтерфейс `BaseNewsAdapter` (data-ingestion/CLAUDE.md,
    "другий інтерфейс — новини").
    """

    source = "sec_filings"

    def __init__(self, user_agent: str, session: Optional[requests.Session] = None):
        if not user_agent:
            raise ValueError(
                "SEC EDGAR вимагає контактний User-Agent (SEC_EDGAR_USER_AGENT), "
                "інакше віддає 403"
            )
        self._headers = {"User-Agent": user_agent}
        self._session = session or requests.Session()

    def list_filings(self, cik: int, forms: tuple[str, ...] = WANTED_FORMS,
                     limit: int = 2) -> list[dict]:
        """Останні `limit` форм зі списку `forms` для цього CIK."""
        response = self._session.get(
            SUBMISSIONS_URL.format(cik=cik), headers=self._headers, timeout=30,
        )
        response.raise_for_status()
        recent = response.json().get("filings", {}).get("recent", {})

        required = ("form", "filingDate", "accessionNumber", "primaryDocument")
        missing = [key for key in required if key not in recent]
        if missing:
            raise ValueError(
                f"SEC submissions CIK {cik}: у відповіді немає полів {missing} — "
                "ймовірно змінився формат"
            )

        out = []
        for form, filing_date, accession, document in zip(*(recent[k] for k in required)):
            if form not in forms:
                continue
            out.append({
                "form": form,
                "filing_date": date.fromisoformat(filing_date),
                "accession": accession,
                "document": document,
            })
            if len(out) >= limit:
                break
        return out

    def fetch_document(self, cik: int, accession: str, document: str) -> str:
        url = DOCUMENT_URL.format(
            cik=cik, accession=accession.replace("-", ""), document=document,
        )
        time.sleep(_SECONDS_BETWEEN_REQUESTS)
        response = self._session.get(url, headers=self._headers, timeout=60)
        response.raise_for_status()
        return response.text

    def collect(self, ticker: str, cik: int, limit: int = 1) -> list[Filing]:
        """Останні `limit` звітів тикера з уже розібраним текстом."""
        out: list[Filing] = []
        for meta in self.list_filings(cik, limit=limit):
            html = self.fetch_document(cik, meta["accession"], meta["document"])
            text = html_to_text(html)
            sections = {}
            for name, (start_pattern, end_patterns) in SECTIONS.items():
                body = extract_section(text, start_pattern, end_patterns)
                if body:
                    sections[name] = body
                else:
                    logger.warning(
                        "%s %s %s: розділ %s не знайдено (або коротший за %d символів)",
                        ticker, meta["form"], meta["filing_date"], name, MIN_SECTION_CHARS,
                    )
            if not sections:
                raise ValueError(
                    f"{ticker} {meta['form']} {meta['filing_date']}: не знайдено "
                    f"ЖОДНОГО змістового розділу у {len(text)} символах тексту — "
                    "ймовірно змінився формат звіту"
                )
            out.append(Filing(
                ticker=ticker,
                cik=cik,
                form=meta["form"],
                filing_date=meta["filing_date"],
                accession=meta["accession"],
                document=meta["document"],
                url=DOCUMENT_URL.format(
                    cik=cik, accession=meta["accession"].replace("-", ""),
                    document=meta["document"],
                ),
                full_text=text,
                sections=sections,
                fetched_at=datetime.now(timezone.utc),
            ))
        return out
