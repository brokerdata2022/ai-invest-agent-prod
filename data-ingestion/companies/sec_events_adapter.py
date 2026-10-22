"""
SEC EDGAR — КОРПОРАТИВНІ ПОДІЇ (8-K) і УГОДИ ІНСАЙДЕРІВ (Form 4) для
акцій списку обраних.

**Чому (2026-10-22, рішення користувача — пункт 1 плану сесії).**
Звітність 10-K/10-Q виходить раз на квартал, а між звітами з компанією
відбувається головне: угоди, зміна керівництва, попередні результати
(8-K), купівлі й продажі власних акцій директорами (Form 4). Живий
кейс SKYD: за останні подання — 34 8-K і 57 Form 4 проти трьох 10-Q;
жодне з них агент не бачив.

**Живий замір ПЕРЕД кодом (2026-10-22), SKYD, CIK 2041610:**
- `submissions/CIK##########.json` → `filings.recent` з полями `form`,
  `accessionNumber`, `filingDate`, `primaryDocument`, `items` (для 8-K
  — коди пунктів через кому: "5.04,9.01", "1.01,2.03,7.01,8.01,9.01");
- Form 4: `primaryDocument` = "xslF345X06/form4.xml" — це HTML-рендер
  через XSL; сирий XML лежить за тим самим шляхом БЕЗ префікса xsl:
  `Archives/edgar/data/2041610/000089534526000470/form4.xml` → 200,
  `<ownershipDocument>` з `reportingOwner` і таблицями
  `nonDerivativeTable`/`derivativeTable`. Живий приклад: директор
  Cardinale Gerald J. 16.10 КУПИВ (код P) 4 900 000 варантів по $2.80 —
  угода в похідній таблиці, тож розбирати треба обидві.

НЕ `BaseAdapter` (результат — події, не числа) — той самий привід, що
`reports/sec_filings_adapter.py`.
"""

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

import requests

logger = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
DOCUMENT_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

EVENT_FORMS = ("8-K", "8-K/A")
INSIDER_FORMS = ("4", "4/A")
_SECONDS_BETWEEN_REQUESTS = 0.2

# Пункти 8-K (Form 8-K General Instructions, SEC) — людською мовою для
# промпта й повідомлень. 9.01 ("фінансова звітність і додатки") —
# службовий пункт, сам по собі події не означає.
ITEM_LABELS: dict[str, str] = {
    "1.01": "суттєва угода",
    "1.02": "розірвання суттєвої угоди",
    "1.03": "банкрутство чи ліквідація",
    "1.05": "кіберінцидент",
    "2.01": "завершення придбання чи продажу активів",
    "2.02": "результати діяльності (звітність)",
    "2.03": "нове боргове зобовʼязання",
    "2.04": "дострокове погашення боргу",
    "2.05": "реструктуризація / скорочення",
    "2.06": "знецінення активів",
    "3.01": "проблема з лістингом",
    "3.02": "продаж акцій без реєстрації",
    "3.03": "зміна прав акціонерів",
    "4.01": "зміна аудитора",
    "4.02": "відкликання попередньої звітності",
    "5.01": "зміна контролю над компанією",
    "5.02": "зміна директорів чи керівництва",
    "5.03": "зміна статуту",
    "5.04": "тимчасова заборона угод у пенсійних планах",
    "5.05": "зміна етичного кодексу",
    "5.06": "вихід зі статусу shell-компанії",
    "5.07": "голосування акціонерів",
    "5.08": "висунення кандидатів у директори акціонерами",
    "7.01": "розкриття для інвесторів (Reg FD)",
    "8.01": "інша суттєва подія",
    "9.01": "фінансові додатки",
}

# Коди угод Form 4 (SEC, Form 4 General Instructions). Ринкові
# сигнали — лише P і S (угода на відкритому ринку за рішенням
# інсайдера); решта — з емітентом чи службові (грант, опціон, податок).
TRANSACTION_LABELS: dict[str, str] = {
    "P": "купівля на ринку",
    "S": "продаж на ринку",
    # "A" за SEC — грант, нагорода АБО інше придбання в емітента (Rule
    # 16b-3(d)); живо SKYD 06.10.2026: директор отримав 175 млн акцій по
    # $12 — це придбання в емітента на $2.1 млрд, а не "грант за графіком".
    "A": "грант або придбання в емітента",
    "M": "виконання опціону",
    "F": "утримання акцій на податок",
    "G": "дарування",
    "D": "повернення емітенту",
    "C": "конвертація",
    "X": "виконання деривативу",
    "J": "інша угода",
}
MARKET_CODES = frozenset({"P", "S"})


@dataclass
class CompanyEvent:
    ticker: str
    cik: int
    form: str
    accession: str
    filing_date: date
    items: list[str]
    document: str
    url: str


@dataclass
class InsiderTransaction:
    ticker: str
    cik: int
    accession: str
    seq: int
    filing_date: date
    owner_name: str
    owner_role: str
    security: str
    derivative: bool
    transaction_date: Optional[date]
    code: str
    shares: Optional[Decimal]
    price: Optional[Decimal]
    acquired_disposed: str
    owned_after: Optional[Decimal]
    raw: dict = field(default_factory=dict)


def _decimal(text: Optional[str]) -> Optional[Decimal]:
    if text is None or not text.strip():
        return None
    try:
        return Decimal(text.strip())
    except InvalidOperation:
        return None


def _date(text: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat((text or "").strip()[:10])
    except ValueError:
        return None


def _value(node: Optional[ET.Element], path: str) -> Optional[str]:
    """Значення поля Form 4: `<transactionShares><value>…</value>` або
    просто текст елемента."""
    if node is None:
        return None
    found = node.find(path)
    if found is None:
        return None
    inner = found.find("value")
    text = inner.text if inner is not None else found.text
    return text.strip() if text else None


def _owner_role(relationship: Optional[ET.Element]) -> str:
    if relationship is None:
        return "інсайдер"
    roles = []
    if (_value(relationship, "isDirector") or "").lower() in ("true", "1"):
        roles.append("директор")
    if (_value(relationship, "isOfficer") or "").lower() in ("true", "1"):
        title = _value(relationship, "officerTitle")
        roles.append(f"керівник ({title})" if title else "керівник")
    if (_value(relationship, "isTenPercentOwner") or "").lower() in ("true", "1"):
        roles.append("власник 10%+")
    return ", ".join(roles) or "інсайдер"


def parse_form4(xml_text: str, ticker: str, cik: int, accession: str, filing_date: date) -> list[InsiderTransaction]:
    """Угоди одного Form 4 — і прямі (акції), і похідні (опціони,
    варанти). Падіння розбору — `ValueError`: тихий порожній список тут
    гірший за помилку (той самий урок, що мʼякий троттл GDELT)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"Form 4 {accession}: не XML ({exc})") from exc
    if root.tag != "ownershipDocument":
        raise ValueError(f"Form 4 {accession}: несподіваний корінь {root.tag!r}")

    owners = root.findall("reportingOwner")
    owner_name = "; ".join(
        (o.findtext("reportingOwnerId/rptOwnerName") or "").strip() for o in owners
    ) or "невідомий"
    owner_role = _owner_role(owners[0].find("reportingOwnerRelationship") if owners else None)

    out: list[InsiderTransaction] = []
    for derivative, table, row_tag in (
        (False, "nonDerivativeTable", "nonDerivativeTransaction"),
        (True, "derivativeTable", "derivativeTransaction"),
    ):
        for row in root.findall(f"{table}/{row_tag}"):
            code = _value(row, "transactionCoding/transactionCode") or ""
            out.append(InsiderTransaction(
                ticker=ticker, cik=cik, accession=accession, seq=len(out), filing_date=filing_date,
                owner_name=owner_name, owner_role=owner_role,
                security=_value(row, "securityTitle") or "",
                derivative=derivative,
                transaction_date=_date(_value(row, "transactionDate")),
                code=code,
                shares=_decimal(_value(row, "transactionAmounts/transactionShares")),
                price=_decimal(_value(row, "transactionAmounts/transactionPricePerShare")),
                acquired_disposed=_value(row, "transactionAmounts/transactionAcquiredDisposedCode") or "",
                owned_after=_decimal(_value(row, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                raw={"code_label": TRANSACTION_LABELS.get(code, code)},
            ))
    return out


def events_from_submissions(payload: dict, ticker: str, cik: int, since: date,
                            forms: tuple[str, ...]) -> list[dict]:
    """Подання потрібних форм від `since` — з `filings.recent`. Поля
    перевіряються явно: зміна формату SEC мусить падати, а не давати
    "подій не було"."""
    try:
        recent = payload["filings"]["recent"]
        columns = [recent[k] for k in ("form", "accessionNumber", "filingDate", "primaryDocument", "items")]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"SEC submissions {ticker}: несподівана структура ({exc})") from exc

    out = []
    for form, accession, filed, document, items in zip(*columns):
        if form not in forms:
            continue
        filing_date = _date(filed)
        if filing_date is None or filing_date < since:
            continue
        out.append({
            "form": form, "accession": accession, "filing_date": filing_date,
            "document": document or "", "items": [i.strip() for i in (items or "").split(",") if i.strip()],
        })
    return out


class SecEventsAdapter:
    def __init__(self, user_agent: str, session: Optional[requests.Session] = None):
        if not user_agent:
            raise ValueError("SEC_EDGAR_USER_AGENT не задано — SEC віддає 403 без контакту")
        self.user_agent = user_agent
        self.session = session or requests.Session()

    def _get(self, url: str) -> requests.Response:
        time.sleep(_SECONDS_BETWEEN_REQUESTS)
        response = self.session.get(url, headers={"User-Agent": self.user_agent}, timeout=30)
        response.raise_for_status()
        return response

    def submissions(self, cik: int) -> dict:
        return self._get(SUBMISSIONS_URL.format(cik=int(cik))).json()

    def events(self, payload: dict, ticker: str, cik: int, since: date) -> list[CompanyEvent]:
        out = []
        for item in events_from_submissions(payload, ticker, cik, since, EVENT_FORMS):
            accession = item["accession"]
            out.append(CompanyEvent(
                ticker=ticker, cik=int(cik), form=item["form"], accession=accession,
                filing_date=item["filing_date"], items=item["items"], document=item["document"],
                url=DOCUMENT_URL.format(cik=int(cik), accession=accession.replace("-", ""), document=item["document"]),
            ))
        return out

    def insider_transactions(self, payload: dict, ticker: str, cik: int, since: date,
                             skip_accessions: frozenset = frozenset()) -> list[InsiderTransaction]:
        """Угоди всіх Form 4 від `since`, крім уже збережених
        (`skip_accessions`) — кожен Form 4 це окремий запит до SEC."""
        out: list[InsiderTransaction] = []
        for item in events_from_submissions(payload, ticker, cik, since, INSIDER_FORMS):
            if item["accession"] in skip_accessions:
                continue
            # "xslF345X06/form4.xml" — рендер; сирий XML — без префікса.
            raw_document = item["document"].split("/")[-1]
            url = DOCUMENT_URL.format(
                cik=int(cik), accession=item["accession"].replace("-", ""), document=raw_document,
            )
            try:
                xml_text = self._get(url).text
                out.extend(parse_form4(xml_text, ticker, int(cik), item["accession"], item["filing_date"]))
            except (requests.exceptions.RequestException, ValueError):
                logger.error("%s: Form 4 %s не розібрано", ticker, item["accession"], exc_info=True)
        return out


def describe_items(items: list[str]) -> str:
    """'1.01,2.03,9.01' → 'суттєва угода; нове боргове зобовʼязання'
    (службовий 9.01 опускається, якщо є щось інше)."""
    meaningful = [i for i in items if i != "9.01"] or items
    return "; ".join(ITEM_LABELS.get(i, f"пункт {i}") for i in meaningful)
