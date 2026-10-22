"""Звітність за подією earnings (2026-10-22, пункт 3 плану сесії)."""

from datetime import date

from collect_reported_filings import find_report_filing

FILINGS = [  # найновіші першими, як віддає SEC
    {"form": "10-Q", "filing_date": date(2026, 10, 23), "accession": "q3"},
    {"form": "10-Q", "filing_date": date(2026, 7, 25), "accession": "q2"},
]


def test_report_filing_after_earnings_is_found():
    assert find_report_filing(FILINGS, date(2026, 10, 21))["accession"] == "q3"


def test_filing_same_morning_or_day_before_still_counts():
    # 10-Q іноді подають напередодні прес-релізу (EARNINGS_FILING_EARLY_DAYS)
    assert find_report_filing(FILINGS, date(2026, 10, 24))["accession"] == "q3"


def test_previous_quarter_filing_is_not_the_new_report():
    """Головна пастка: старий 10-Q не мусить читатись як "звіт уже
    подано" — тоді компанія чекала б до неділі, як і раніше."""
    assert find_report_filing(FILINGS[1:], date(2026, 10, 21)) is None
