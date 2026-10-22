#!/usr/bin/env python3
"""
Дані компанії понад квартальну звітність для АКЦІЙ СПИСКУ ОБРАНИХ:
корпоративні події (8-K), угоди інсайдерів (Form 4), консенсус
аналітиків і free float (три джерела для звірки).

Квартальний фундаментал SEC збирає `collect_companies_universe.py`,
текст 10-K/10-Q — `analysis/fundamental/analyze_filings.py`; тут —
те, що змінюється між звітами (пункт 1 плану сесії 2026-10-22).

Джерело, що впало, не зупиняє решту; але якщо впало хоч одне —
код виходу 1 (урок GDELT: тихий "0 записів" неприпустимий).

Використання:
    python collect_company_data.py                 # усі акції списку
    python collect_company_data.py --ticker SKYD
"""

import argparse
import logging
import os
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from common.db import get_connection, insert_observations  # noqa: E402
from common.watchlist_db import fetch_stocks  # noqa: E402
from companies.analyst_adapters import ANALYST_ADAPTERS  # noqa: E402
from companies.sec_edgar_adapter import lookup_company  # noqa: E402
from companies.sec_events_adapter import SecEventsAdapter  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def save_events(conn, events) -> int:
    inserted = 0
    with conn.cursor() as cur:
        for e in events:
            cur.execute(
                """
                INSERT INTO company_events (accession, ticker, cik, form, filing_date, items, url)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (accession) DO NOTHING
                """,
                (e.accession, e.ticker, e.cik, e.form, e.filing_date, e.items, e.url),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted


def known_insider_accessions(conn, ticker: str) -> frozenset:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT accession FROM insider_transactions WHERE ticker = %s", (ticker,))
        return frozenset(row[0] for row in cur.fetchall())


def save_insider_transactions(conn, transactions) -> int:
    inserted = 0
    with conn.cursor() as cur:
        for t in transactions:
            cur.execute(
                """
                INSERT INTO insider_transactions
                    (accession, seq, ticker, cik, filing_date, owner_name, owner_role, security,
                     derivative, transaction_date, code, shares, price, acquired_disposed, owned_after)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (accession, seq) DO NOTHING
                """,
                (t.accession, t.seq, t.ticker, t.cik, t.filing_date, t.owner_name, t.owner_role,
                 t.security, t.derivative, t.transaction_date, t.code, t.shares, t.price,
                 t.acquired_disposed, t.owned_after),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted


def collect_ticker(conn, ticker: str, cik, user_agent: str) -> list[str]:
    """Повертає назви джерел, що впали."""
    failed: list[str] = []

    if cik is None:
        company = lookup_company(ticker, user_agent)
        cik = company["cik"] if company else None
    if cik is None:
        logger.error("%s: CIK невідомий — 8-K і Form 4 пропущено", ticker)
        failed.append("sec_cik")
    else:
        adapter = SecEventsAdapter(user_agent)
        try:
            payload = adapter.submissions(int(cik))
            since_events = date.today() - timedelta(days=config.COMPANY_EVENTS_LOOKBACK_DAYS)
            events = adapter.events(payload, ticker, int(cik), since_events)
            logger.info("%s: 8-K — %d подій, %d нових", ticker, len(events), save_events(conn, events))

            since_insiders = date.today() - timedelta(days=config.INSIDER_LOOKBACK_DAYS)
            transactions = adapter.insider_transactions(
                payload, ticker, int(cik), since_insiders,
                skip_accessions=known_insider_accessions(conn, ticker),
            )
            logger.info(
                "%s: Form 4 — %d угод, %d нових",
                ticker, len(transactions), save_insider_transactions(conn, transactions),
            )
        except Exception:
            logger.error("%s: SEC 8-K/Form 4 провалились", ticker, exc_info=True)
            conn.rollback()
            failed.append("sec_events")

    for adapter_class in ANALYST_ADAPTERS:
        try:
            records = adapter_class(ticker).collect()
            inserted = insert_observations(conn, records)
            logger.info("%s: %s — %d полів (%d нових/змінених)", ticker, adapter_class.source, len(records), inserted)
        except Exception:
            logger.error("%s: %s провалився", ticker, adapter_class.source, exc_info=True)
            conn.rollback()
            failed.append(adapter_class.source)
    return failed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ticker", default=None)
    args = parser.parse_args()
    load_dotenv()

    user_agent = os.environ.get("SEC_EDGAR_USER_AGENT")
    if not user_agent:
        sys.exit("SEC_EDGAR_USER_AGENT не задано — SEC віддає 403 без контакту")

    conn = get_connection()
    try:
        stocks = fetch_stocks(conn)
        if args.ticker:
            wanted = args.ticker.upper()
            match = [s for s in stocks if s["ticker"].upper() == wanted]
            targets = [(wanted, match[0].get("cik") if match else None)]
        else:
            targets = [(s["ticker"].upper(), s.get("cik")) for s in stocks]
        if not targets:
            logger.info("Акцій у списку обраних немає — нічого збирати")
            return

        failures = {}
        for ticker, cik in targets:
            failed = collect_ticker(conn, ticker, cik, user_agent)
            if failed:
                failures[ticker] = failed
    finally:
        conn.close()

    if failures:
        logger.error("Провалились: %s", "; ".join(f"{t}: {', '.join(f)}" for t, f in failures.items()))
        sys.exit(1)
    logger.info("Готово: %d акцій", len(targets))


if __name__ == "__main__":
    main()
