"""
8-K і Form 4 (`companies/sec_events_adapter.py`, 2026-10-22) — на
фікстурах, знятих із живих подань SKYD (CIK 2041610) того дня.
"""

from datetime import date
from decimal import Decimal

import pytest

from companies.sec_events_adapter import (
    describe_items,
    events_from_submissions,
    parse_form4,
)

# Скорочена жива Form 4: директор купив варанти (похідна таблиця) —
# саме угода, яку легко пропустити, розбираючи лише прямі акції.
FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>
    <issuer><issuerCik>0002041610</issuerCik><issuerTradingSymbol>SKYD</issuerTradingSymbol></issuer>
    <reportingOwner>
        <reportingOwnerId><rptOwnerName>Cardinale Gerald J.</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>true</isDirector><isOfficer>false</isOfficer></reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle><value>Class B common stock</value></securityTitle>
            <transactionDate><value>2026-10-15</value></transactionDate>
            <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>1000</value></transactionShares>
                <transactionPricePerShare><value>7.26</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts><sharesOwnedFollowingTransaction><value>290357</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
    <derivativeTable>
        <derivativeTransaction>
            <securityTitle><value>Warrants</value></securityTitle>
            <transactionDate><value>2026-10-16</value></transactionDate>
            <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>4900000</value></transactionShares>
                <transactionPricePerShare><value>2.80</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </derivativeTransaction>
    </derivativeTable>
</ownershipDocument>"""


def test_parse_form4_reads_direct_and_derivative_transactions():
    rows = parse_form4(FORM4, "SKYD", 2041610, "0000895345-26-000470", date(2026, 10, 20))
    assert [(r.code, r.derivative) for r in rows] == [("S", False), ("P", True)]
    buy = rows[1]
    assert buy.owner_name == "Cardinale Gerald J."
    assert buy.owner_role == "директор"
    assert buy.security == "Warrants"
    assert buy.shares == Decimal("4900000") and buy.price == Decimal("2.80")
    assert buy.transaction_date == date(2026, 10, 16)
    assert rows[0].owned_after == Decimal("290357")


def test_parse_form4_fails_loudly_on_non_form4():
    """Тихий порожній список гірший за помилку (урок мʼякого троттла GDELT)."""
    with pytest.raises(ValueError):
        parse_form4("<html>not found</html>", "SKYD", 1, "x", date(2026, 10, 20))


SUBMISSIONS = {"filings": {"recent": {
    "form": ["4", "8-K", "10-Q", "8-K"],
    "accessionNumber": ["a1", "a2", "a3", "a4"],
    "filingDate": ["2026-10-20", "2026-10-13", "2026-08-04", "2026-05-01"],
    "primaryDocument": ["xslF345X06/form4.xml", "k.htm", "q.htm", "old.htm"],
    "items": ["", "1.01,2.03,9.01", "", "2.02,9.01"],
}}}


def test_events_from_submissions_filters_form_and_date():
    events = events_from_submissions(SUBMISSIONS, "SKYD", 2041610, date(2026, 9, 1), ("8-K", "8-K/A"))
    assert [e["accession"] for e in events] == ["a2"]
    assert events[0]["items"] == ["1.01", "2.03", "9.01"]


def test_events_from_submissions_fails_on_changed_format():
    with pytest.raises(ValueError):
        events_from_submissions({"filings": {}}, "SKYD", 1, date(2026, 1, 1), ("8-K",))


def test_describe_items_in_plain_language_without_service_item():
    assert describe_items(["1.01", "2.03", "9.01"]) == "суттєва угода; нове боргове зобовʼязання"
    assert describe_items(["9.01"]) == "фінансові додатки"
