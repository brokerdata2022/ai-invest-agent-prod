#!/usr/bin/env python3
"""
ДІАГНОСТИКА: чому список скринінгу майже не змінюється від дня до дня
(пункт 3 плану сесії 2026-10-22). Лише читає БД — нічого не пише, ШІ
не викликає.

Три виміри — щоб причину назвати числом, а не здогадом:

1. **Склад між прогонами.** Скільки тикерів додалось/випало, частка
   спільних (Jaccard), перетин топ-10, середня зміна скору. Якщо склад
   стоїть, а скор рухається — воронка (Tier A/B/C) жорстка, ранжування
   живе. Якщо стоїть і те, і те — входи не оновлюються.
2. **Давність звітності.** Для тикерів останнього прогону — кінець
   останнього кварталу в SEC (`{t}_revenue`) і коли його забрали.
   Велика частка кварталів старших за ~100 днів означає, що скринінг
   працює на минулому кварталі.
3. **Звіт вийшов — цифр немає.** Тикери, що за `earnings_calendar`
   ВЖЕ відзвітували, але в SEC-даних досі квартал до звіту. Саме це
   лікує `earnings_filings` (collect_reported_filings.py); до неї
   такий тикер чекав неділі.

Використання:
    python diagnose_stability.py
    python diagnose_stability.py --runs 14
"""

import argparse
import os
import statistics
import sys
from datetime import date

from dotenv import load_dotenv

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ANALYSIS_DIR)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))

from common.db import get_connection  # noqa: E402

# Квартал звітності ~91 день + строк подання 10-Q до 45 днів: кінець
# останнього кварталу, старший за це, — минулий квартал.
_STALE_QUARTER_DAYS = 136


def fetch_runs(conn, runs: int) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_at, array_agg(ticker ORDER BY score DESC), array_agg(score ORDER BY score DESC)
            FROM screening_results
            WHERE run_at IN (SELECT DISTINCT run_at FROM screening_results ORDER BY run_at DESC LIMIT %s)
            GROUP BY run_at
            ORDER BY run_at
            """,
            (runs,),
        )
        return cur.fetchall()


def membership_report(runs: list[tuple]) -> list[str]:
    lines = ["1. СКЛАД МІЖ ПРОГОНАМИ"]
    if len(runs) < 2:
        return lines + ["   менше двох прогонів — порівнювати нічого"]
    jaccards, top_overlaps, score_moves = [], [], []
    for (prev_at, prev_t, prev_s), (cur_at, cur_t, cur_s) in zip(runs, runs[1:]):
        prev_set, cur_set = set(prev_t), set(cur_t)
        jaccard = len(prev_set & cur_set) / len(prev_set | cur_set) if prev_set | cur_set else 1.0
        top = len(set(prev_t[:10]) & set(cur_t[:10]))
        prev_scores = dict(zip(prev_t, prev_s))
        moves = [abs(float(s) - float(prev_scores[t])) for t, s in zip(cur_t, cur_s) if t in prev_scores]
        move = statistics.mean(moves) if moves else 0.0
        jaccards.append(jaccard)
        top_overlaps.append(top)
        score_moves.append(move)
        lines.append(
            f"   {cur_at:%d.%m %H:%M}: {len(cur_set)} тикерів, +{len(cur_set - prev_set)} −{len(prev_set - cur_set)}, "
            f"спільних {jaccard:.0%}, топ-10 спільних {top}/10, сер. |Δскор| {move:.4f}"
        )
    lines.append(
        f"   РАЗОМ: спільних у середньому {statistics.mean(jaccards):.0%}, "
        f"топ-10 — {statistics.mean(top_overlaps):.1f}/10, |Δскор| {statistics.mean(score_moves):.4f}"
    )
    return lines


def freshness_report(conn, tickers: list[str], today: date) -> list[str]:
    lines = ["2. ДАВНІСТЬ ЗВІТНОСТІ (тикери останнього прогону)"]
    if not tickers:
        return lines + ["   прогонів немає"]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT upper(split_part(metric_id, '_revenue', 1)), MAX(observed_at), MAX(fetched_at)
            FROM raw_observations
            WHERE source = 'sec_edgar' AND metric_id = ANY(%s)
            GROUP BY 1
            """,
            ([f"{t.lower()}_revenue" for t in tickers],),
        )
        rows = {t: (period_end, fetched) for t, period_end, fetched in cur.fetchall()}
    ages = [(today - period_end).days for period_end, _f in rows.values()]
    fetched_ages = [(today - fetched.date()).days for _p, fetched in rows.values()]
    stale = sum(1 for a in ages if a > _STALE_QUARTER_DAYS)
    lines += [
        f"   виручка в SEC є в {len(rows)} із {len(tickers)} тикерів",
        f"   кінець останнього кварталу: медіана {statistics.median(ages) if ages else '—'} дн тому; "
        f"старших за {_STALE_QUARTER_DAYS} дн (минулий квартал) — {stale}",
        f"   коли забрали востаннє: медіана {statistics.median(fetched_ages) if fetched_ages else '—'} дн тому, "
        f"найдавніше {max(fetched_ages) if fetched_ages else '—'} дн",
    ]
    return lines


def reported_without_numbers(conn, today: date) -> list[str]:
    lines = ["3. ЗВІТ ВИЙШОВ — ЦИФР У SEC-ДАНИХ НЕМАЄ (останні 60 дн)"]
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH reported AS (
                SELECT upper(ticker) AS ticker, MAX(report_date) AS report_date
                FROM earnings_calendar
                WHERE report_date BETWEEN %s::date - 60 AND %s::date - 1
                GROUP BY 1
            ), latest AS (
                SELECT upper(split_part(metric_id, '_revenue', 1)) AS ticker,
                       MAX(observed_at) AS period_end, MAX(fetched_at) AS fetched
                FROM raw_observations
                WHERE source = 'sec_edgar' AND metric_id LIKE '%%\\_revenue'
                GROUP BY 1
            )
            SELECT r.ticker, r.report_date, l.period_end, l.fetched
            FROM reported r LEFT JOIN latest l USING (ticker)
            WHERE l.period_end IS NULL OR l.period_end < r.report_date - 100
            ORDER BY r.report_date
            """,
            (today, today),
        )
        rows = cur.fetchall()
    if not rows:
        return lines + ["   таких немає"]
    lines.append(f"   {len(rows)} тикерів; найдавніші:")
    for ticker, report_date, period_end, fetched in rows[:15]:
        if fetched is None:
            lines.append(f"   {ticker}: звіт {report_date}, звітності SEC у нас немає взагалі")
        else:
            lines.append(f"   {ticker}: звіт {report_date}, у нас квартал до {period_end} (забрали {fetched:%d.%m})")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()
    load_dotenv()

    today = date.today()
    conn = get_connection()
    try:
        runs = fetch_runs(conn, args.runs)
        latest = list(runs[-1][1]) if runs else []
        report = membership_report(runs) + [""] + freshness_report(conn, latest, today) + [""] + reported_without_numbers(conn, today)
    finally:
        conn.close()
    print("\n".join(report))


if __name__ == "__main__":
    main()
