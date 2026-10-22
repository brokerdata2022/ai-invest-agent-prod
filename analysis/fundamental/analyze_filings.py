#!/usr/bin/env python3
"""
Збір тексту звітів 10-K/10-Q + LLM-витяг тез.

**Навіщо.** Остання невиконана обіцянка опису агента ("збирає ...
звіти компаній"): числовий XBRL збирався з початку, текст звіту — ні.
Числа кажуть ЩО сталося, MD&A каже ЧОМУ. Живий привід —
фундаментальний аналіз 12 жовтня: він переказував квартальні цифри
("виручка зросла з 3.69 до 5.04 млрд") і не міг назвати причину,
хоча в звіті вона написана словами.

**Чому LLM, а не правила.** Витягти «які чинники керівництво саме
називає драйверами» з 20 КБ юридично вилизаного тексту — задача на
розуміння мови, не на регулярки. Це рівно той клас, де LLM виправданий
(analysis/CLAUDE.md: числові порівняння — звичайний код, тлумачення
тексту — модель).

**Універсум — топ скринінгу, не весь S&P 500.** 503 звіти × 1 МБ ×
LLM-виклик на кожен — це дорого й непотрібно: тези потрібні саме для
тих тикерів, які агент і так виводить користувачу. Беремо топ
`screening_results`, та сама логіка, що в `run_analysis.py`.

Використання:
    python analyze_filings.py --top 5
    python analyze_filings.py --ticker AAPL
    python analyze_filings.py --top 5 --collect-only   # без LLM
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ANALYSIS_DIR)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))

from common.db import get_connection  # noqa: E402
from config import (  # noqa: E402
    FILING_INSIGHT_MAX_SECTION_CHARS,
    FILING_INSIGHT_TOP_TICKERS,
)
from llm_common import (  # noqa: E402
    LlmError,
    call_llm,
    log_llm_call,
    parse_json_object,
    require_api_key,
)
from reports.sec_filings_adapter import SecFilingsAdapter  # noqa: E402
from screening._results_db import fetch_latest_tickers  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PURPOSE = "filing_insight"

SYSTEM_PROMPT = (
    "Ти фінансовий аналітик. Тобі дають фрагменти ОФІЦІЙНОГО звіту "
    "компанії до SEC (10-K або 10-Q): розділ MD&A (пояснення "
    "керівництва) і Risk Factors. Твоє завдання — витягти ТЕЗИ, а не "
    "переказати текст.\n\n"
    "Не давай інвестиційних порад і не кажи, що робити з активом — "
    "лише що САМА КОМПАНІЯ називає причинами й ризиками.\n\n"
    "Особливо цінне: що змінилось відносно попереднього періоду "
    "(нові ризики, зняті ризики, нові формулювання про попит/маржу/"
    "витрати) — бо саме зміна формулювань, а не його наявність, несе "
    "інформацію.\n\n"
    "Відповідай ЛИШЕ JSON-об'єктом з полями: summary (2-3 речення: "
    "головне зі звіту своїми словами), drivers (масив рядків: чинники, "
    "які керівництво називає причинами результату), risks (масив "
    "рядків: ризики, які компанія вважає суттєвими — лише ті, що "
    "конкретні для неї, не загальні кліше), changes (масив рядків: що "
    "змінилось у формулюваннях відносно попереднього звіту; порожній "
    "масив, якщо зі самого тексту це не видно), direction (одне з: up, "
    "down, neutral, unclear — як текст звіту характеризує напрямок "
    "бізнесу), confidence (0..1 — наскільки текст дає чіткий сигнал)."
)


def build_prompt(filing, sections: dict) -> str:
    lines = [
        f"Компанія: {filing['ticker']}",
        f"Форма: {filing['form']}, подано {filing['filing_date']}",
        "",
    ]
    for name, label in (("mdna", "MD&A (пояснення керівництва)"),
                        ("risk_factors", "Risk Factors")):
        body = sections.get(name)
        if not body:
            continue
        # Обрізаємо кожен розділ: 20+20 КБ це ~10k токенів, і довший
        # хвіст у звітах SEC — переважно юридичні застереження, що
        # повторюються дослівно з кварталу в квартал.
        lines.append(f"--- {label} ---")
        lines.append(body[:FILING_INSIGHT_MAX_SECTION_CHARS])
        lines.append("")
    return "\n".join(lines)


def extract_insight(filing: dict, sections: dict):
    prompt = build_prompt(filing, sections)
    # Порядок аргументів — (prompt, system_prompt, purpose), як у
    # llm_common.call_llm; повертається LlmResponse, не кортеж.
    llm = call_llm(prompt, SYSTEM_PROMPT, purpose=PURPOSE)
    data = parse_json_object(llm.text, error_class=ValueError)

    if "summary" not in data:
        raise ValueError(f"У відповіді LLM немає 'summary': {data!r}")

    def as_list(key):
        value = data.get(key) or []
        return [str(x) for x in value] if isinstance(value, list) else [str(value)]

    return {
        "summary": str(data["summary"]),
        "drivers": as_list("drivers"),
        "risks": as_list("risks"),
        "changes": as_list("changes"),
        "direction": data.get("direction"),
        "confidence": data.get("confidence"),
    }, prompt, llm


def save_filing(conn, filing) -> int:
    """Повертає id рядка. ON CONFLICT — той самий звіт не
    перезаписується: текст звіту незмінний назавжди (правило 6)."""
    import json

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO company_filings
                (ticker, cik, form, filing_date, accession, url, full_text, sections)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (accession, ticker) DO UPDATE
                SET sections = EXCLUDED.sections
            RETURNING id
            """,
            (
                filing.ticker, filing.cik, filing.form, filing.filing_date,
                filing.accession, filing.url, filing.full_text,
                json.dumps(filing.sections, ensure_ascii=False),
            ),
        )
        filing_id = cur.fetchone()[0]
    conn.commit()
    return filing_id


def save_insight(conn, filing_id: int, ticker: str, insight: dict, llm_call_id) -> None:
    import json

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO filing_insights
                (filing_id, ticker, summary, drivers, risks, changes,
                 direction, confidence, llm_call_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (filing_id) DO UPDATE SET
                summary = EXCLUDED.summary,
                drivers = EXCLUDED.drivers,
                risks = EXCLUDED.risks,
                changes = EXCLUDED.changes,
                direction = EXCLUDED.direction,
                confidence = EXCLUDED.confidence,
                llm_call_id = EXCLUDED.llm_call_id,
                notified_at = NULL,
                created_at = now()
            """,
            (
                filing_id, ticker, insight["summary"],
                json.dumps(insight["drivers"], ensure_ascii=False),
                json.dumps(insight["risks"], ensure_ascii=False),
                json.dumps(insight["changes"], ensure_ascii=False),
                insight["direction"], insight["confidence"], llm_call_id,
            ),
        )
    conn.commit()


def already_analyzed(conn, accession: str, ticker: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM filing_insights fi
            JOIN company_filings cf ON cf.id = fi.filing_id
            WHERE cf.accession = %s AND cf.ticker = %s
            """,
            (accession, ticker),
        )
        return cur.fetchone() is not None


def watchlist_stock_tickers(conn) -> list[str]:
    """Тикери акцій СПИСКУ ОБРАНИХ (2026-10-22): їхні звіти розбираються
    завжди, а не лише коли тикер у топі скринінгу — інакше акція поза
    S&P 500 (SKYD) тез зі звіту не мала б ніколи."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT upper(ticker) FROM watchlist_assets
            WHERE enabled AND asset_class = 'stock' AND ticker IS NOT NULL
            ORDER BY 1
            """
        )
        return [row[0] for row in cur.fetchall()]


def resolve_cik(conn, ticker: str):
    """CIK із уже зібраного фундаменталу — другий мережевий лукап
    непотрібен.

    Витягуємо з `accn` (accession number), а не з окремого поля `cik`:
    того поля в `raw_payload` немає (перевірено живим рядком), зате
    ПЕРШІ 10 ЦИФР номера подання — це і є CIK заявника. Формат
    `0000320193-25-000008` → CIK 320193. Це властивість самого формату
    SEC, не наша евристика.

    Першим — CIK із рядка активу списку обраних (2026-10-22): він
    відомий одразу після `/watchlist_add`, ще до першого збору
    фундаменталу."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cik FROM watchlist_assets WHERE upper(ticker) = %s AND cik IS NOT NULL LIMIT 1",
            (ticker.upper(),),
        )
        row = cur.fetchone()
    if row and row[0]:
        return int(row[0])
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT raw_payload->>'accn'
            FROM raw_observations
            WHERE source = 'sec_edgar' AND metric_id LIKE %s
              AND raw_payload->>'accn' IS NOT NULL
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (f"{ticker.lower()}_%",),
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        return int(row[0].split("-")[0])
    except (ValueError, IndexError):
        logger.warning("%s: не вдалось дістати CIK з accn=%r", ticker, row[0])
        return None


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default=None, help="один тикер (інакше топ скринінгу)")
    parser.add_argument("--top", type=int, default=FILING_INSIGHT_TOP_TICKERS)
    parser.add_argument("--limit-per-ticker", type=int, default=1,
                        help="скільки останніх звітів на тикер")
    parser.add_argument("--collect-only", action="store_true",
                        help="лише зібрати текст, без LLM-витягу")
    args = parser.parse_args()

    user_agent = os.environ.get("SEC_EDGAR_USER_AGENT")
    if not user_agent:
        sys.exit("SEC_EDGAR_USER_AGENT не задано — SEC віддає 403 без контакту")

    if not args.collect_only:
        require_api_key(PURPOSE)

    adapter = SecFilingsAdapter(user_agent=user_agent)
    conn = get_connection()
    try:
        if args.ticker:
            tickers = [args.ticker]
        else:
            watchlist_stocks = watchlist_stock_tickers(conn)
            tickers = watchlist_stocks + [
                t for t in fetch_latest_tickers(conn)[: args.top] if t.upper() not in watchlist_stocks
            ]
        if not tickers:
            logger.info("Немає тикерів (скринінг ще не прогонявся) — нічого робити")
            return
        logger.info("Тикерів для розбору звітів: %d (%s)", len(tickers), ", ".join(tickers))

        collected = analyzed = 0
        for ticker in tickers:
            cik = resolve_cik(conn, ticker)
            if cik is None:
                logger.warning("%s: CIK не знайдено в raw_observations — пропущено", ticker)
                continue
            try:
                filings = adapter.collect(ticker, cik, limit=args.limit_per_ticker)
            except Exception:
                logger.exception("%s: збій збору звіту — пропущено", ticker)
                continue

            for filing in filings:
                if already_analyzed(conn, filing.accession, ticker) and not args.collect_only:
                    logger.info(
                        "%s %s %s: тези вже витягнуті — пропущено",
                        ticker, filing.form, filing.filing_date,
                    )
                    continue

                filing_id = save_filing(conn, filing)
                collected += 1
                logger.info(
                    "%s %s %s: текст збережено (%d КБ, розділи: %s)",
                    ticker, filing.form, filing.filing_date,
                    len(filing.full_text) // 1024, ", ".join(filing.sections),
                )
                if args.collect_only:
                    continue

                row = {
                    "ticker": ticker, "form": filing.form,
                    "filing_date": filing.filing_date,
                }
                try:
                    insight, prompt, llm = extract_insight(row, filing.sections)
                except (LlmError, ValueError):
                    logger.exception("%s: витяг тез провалився — пропущено", ticker)
                    continue

                llm_call_id = log_llm_call(
                    conn, llm=llm, purpose=PURPOSE, prompt=prompt,
                    source_ref=f"{ticker}:{filing.accession}",
                )
                save_insight(conn, filing_id, ticker, insight, llm_call_id)
                analyzed += 1
                logger.info(
                    "%s: тези — %s (напрямок %s): %s",
                    ticker, f"{len(insight['drivers'])} драйверів/"
                    f"{len(insight['risks'])} ризиків/{len(insight['changes'])} змін",
                    insight["direction"], insight["summary"][:140],
                )

        logger.info("Готово: зібрано %d звітів, витягнуто тез %d", collected, analyzed)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
