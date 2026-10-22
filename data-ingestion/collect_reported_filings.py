#!/usr/bin/env python3
"""
Звітність ЗА ПОДІЄЮ: компанія відзвітувала (`earnings_calendar`) →
щойно її 10-Q/10-K зʼявився в SEC, забрати фундаментал (XBRL).

**Чому (2026-10-22, пункт 3 плану сесії).** Ціни S&P 500 оновлюються
щодня о 03:00, скринінг — о 05:00, а фундаментал SEC — щонеділі. У
сезон звітів компанія, що відзвітувала у вівторок, потрапляла в
скринінг зі свіжими цифрами лише в понеділок, хоча її скор на 85%
складається з квартальних даних. Тижнева джоба лишається страховкою.

**Порядок.** Кандидати — звіти за останні `config.EARNINGS_REFRESH_WAIT_DAYS`
днів (до вчора), по яких ще немає рядка в `earnings_refresh_log`.
Для кожного — один запит `submissions` SEC: чи є 10-Q/10-K, поданий не
раніше за дату звіту мінус `EARNINGS_FILING_EARLY_DAYS` (деякі компанії
подають 10-Q того ж ранку). Є — фундаментал тикера через
`SecEdgarAdapter`, рядок 'refreshed'. Немає — перевіримо завтра; після
вікна очікування — 'no_filing' (видно, а не тиша).

Запускається ПЕРЕД ранковим скринінгом (schedule.py), тож окремого
перерахунку скринінгу не потрібно — він іде о 05:00 уже на нових
цифрах; тексти звітів розбирає щоденна `filing_insights` після нього.

Використання:
    python collect_reported_filings.py --dry-run   # лише показати кандидатів і що знайдено
    python collect_reported_filings.py
"""

import argparse
import logging
import os
import sys
import time
from datetime import date, timedelta

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from common.db import get_connection, insert_observations  # noqa: E402
from companies.sec_edgar_adapter import SecEdgarAdapter, ticker_cik_map  # noqa: E402
from reports.sec_filings_adapter import SecFilingsAdapter  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

REPORT_FORMS = ("10-Q", "10-K", "10-Q/A", "10-K/A")
_SECONDS_BETWEEN_TICKERS = 0.15


def fetch_candidates(conn, today: date) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.ticker, e.report_date
            FROM earnings_calendar e
            WHERE e.report_date BETWEEN %s AND %s
              AND NOT EXISTS (
                  SELECT 1 FROM earnings_refresh_log l
                  WHERE l.ticker = e.ticker AND l.report_date = e.report_date
              )
            ORDER BY e.report_date, e.ticker
            """,
            (today - timedelta(days=config.EARNINGS_REFRESH_WAIT_DAYS), today - timedelta(days=1)),
        )
        return [{"ticker": t.upper(), "report_date": d} for t, d in cur.fetchall()]


def find_report_filing(filings: list[dict], report_date: date) -> dict | None:
    """Перше (найсвіжіше) подання 10-Q/10-K не раніше за дату звіту
    мінус допуск — чиста функція."""
    earliest = report_date - timedelta(days=config.EARNINGS_FILING_EARLY_DAYS)
    for filing in filings:
        if filing["filing_date"] >= earliest:
            return filing
    return None


def log_result(conn, ticker: str, report_date: date, status: str, filing: dict | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO earnings_refresh_log (ticker, report_date, status, form, accession, filing_date)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (ticker, report_date) DO NOTHING
            """,
            (ticker, report_date, status,
             filing and filing["form"], filing and filing["accession"], filing and filing["filing_date"]),
        )
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="нічого не записувати")
    args = parser.parse_args()
    load_dotenv()

    user_agent = os.environ.get("SEC_EDGAR_USER_AGENT")
    if not user_agent:
        sys.exit("SEC_EDGAR_USER_AGENT не задано — SEC віддає 403 без контакту")

    today = date.today()
    conn = get_connection()
    refreshed, waiting, gave_up, failed = [], [], [], []
    try:
        candidates = fetch_candidates(conn, today)
        logger.info("Звітів, що чекають на подання в SEC: %d", len(candidates))
        if not candidates:
            return
        ciks = ticker_cik_map(user_agent)
        filings_adapter = SecFilingsAdapter(user_agent=user_agent)

        for item in candidates:
            ticker, report_date = item["ticker"], item["report_date"]
            cik = ciks.get(ticker) or ciks.get(ticker.replace(".", "-"))
            if cik is None:
                logger.warning("%s: CIK невідомий у довіднику SEC — пропущено", ticker)
                failed.append(ticker)
                continue
            # SEC fair-access — 10 запитів/с; у сезон звітів кандидатів
            # сотні, тож пауза тут, а не надія на швидкість мережі.
            time.sleep(_SECONDS_BETWEEN_TICKERS)
            try:
                filings = filings_adapter.list_filings(cik, forms=REPORT_FORMS, limit=3)
                filing = find_report_filing(filings, report_date)
                if filing is None:
                    if (today - report_date).days >= config.EARNINGS_REFRESH_WAIT_DAYS:
                        gave_up.append(ticker)
                        if not args.dry_run:
                            log_result(conn, ticker, report_date, "no_filing", None)
                    else:
                        waiting.append(ticker)
                    continue
                logger.info("%s: звіт %s → %s від %s", ticker, report_date, filing["form"], filing["filing_date"])
                if args.dry_run:
                    refreshed.append(ticker)
                    continue
                records = SecEdgarAdapter(api_key=user_agent, ticker=ticker, cik=str(cik)).collect()
                inserted = insert_observations(conn, records)
                log_result(conn, ticker, report_date, "refreshed", filing)
                logger.info("%s: фундаментал — %d записів (%d нових/змінених)", ticker, len(records), inserted)
                refreshed.append(ticker)
            except Exception:
                logger.error("%s: перевірка подання провалилась", ticker, exc_info=True)
                conn.rollback()
                failed.append(ticker)
    finally:
        conn.close()

    logger.info(
        "Готово: оновлено %d (%s); чекають на 10-Q %d; без подання за %d дн — %d (%s); з помилкою %d (%s)",
        len(refreshed), ", ".join(refreshed) or "—", len(waiting),
        config.EARNINGS_REFRESH_WAIT_DAYS, len(gave_up), ", ".join(gave_up) or "—",
        len(failed), ", ".join(failed) or "—",
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
