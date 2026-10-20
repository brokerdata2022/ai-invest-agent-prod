#!/usr/bin/env python3
"""
Звірка й свіжість ФАКТОРНИХ рядів (`config.FACTOR_SERIES`): запаси
нафти й газу, он-чейн, мідь, погода, потоки в ETF.

**Навіщо (2026-10-20).** Принцип даних із 2026-10-12: правда — це
згода кількох джерел. Факторні ряди йдуть прямо в промпт
фундаментального аналізу, тож хибне чи застигле значення там коштує
хибного висновку. Ця джоба:
1. для кожного ряду з парою бере останню СПІЛЬНУ дату й проганяє
   значення через `common/consensus.py`; для пар `compare='diff'`
   (TradingEconomics публікує ЗМІНУ запасів, а не рівень) порівнює
   її з різницею двох точок основного ряду;
2. пише вердикт у `factor_verdicts` — аналіз ставить його поруч із
   числами (`analysis/fundamental/factors.py`);
3. перевіряє свіжість основного ряду (`common/freshness.py`).

Telegram — ЛИШЕ коли є що сказати: нова розбіжність по точці, яку ще
не повідомляли (і лише для рядів з `alert=True` — розкид двох
прогнозів погоди це невизначеність, а не помилка), або змінився
набір застарілих рядів. Звірка, що щодня звітує "усе гаразд", стає
шумом (той самий принцип, що macro_crosscheck).

Сирі дані не змінюються (правило 6) — звірка лише КАЖЕ.

Використання:
    python crosscheck_factors.py
    python crosscheck_factors.py --notify
"""

import argparse
import json
import logging
import os
import sys
from datetime import date
from decimal import Decimal
from typing import Optional

from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, "data-ingestion"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "reporting"))

import config  # noqa: E402
from common.consensus import AGREE, INSUFFICIENT, ConsensusVerdict, SourceValue, reconcile  # noqa: E402
from common.freshness import is_stale  # noqa: E402
from factor_sources import secondary_pairs  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Скільки останніх точок кожного джерела читати для пошуку спільної
# дати. blockchain.info відстає на ~6 днів, тижневі ряди — по точці на
# тиждень; 60 точок покривають обидва з запасом.
_LOOKBACK_POINTS = 60

STALE_NOTIFIER = "factor_series_stale"


def _diff_at(series: dict[date, Decimal], day: date) -> Optional[Decimal]:
    """Зміна основного ряду на дату `day` відносно попередньої точки."""
    earlier = [d for d in series if d < day]
    if day not in series or not earlier:
        return None
    return series[day] - series[max(earlier)]


def compare_series(
    metric_id: str,
    spec: dict,
    primary: dict[date, Decimal],
    secondaries: list[tuple[dict, dict[date, Decimal]]],
) -> Optional[tuple[date, ConsensusVerdict, dict[str, Decimal]]]:
    """(дата, вердикт, {джерело: значення}) для останньої спільної дати.
    None — основного ряду немає зовсім.

    Без пари — вердикт `insufficient` на останню точку: це не збій, а
    чесне "одне джерело", і аналіз мусить це бачити."""
    if not primary:
        return None

    if not secondaries:
        latest = max(primary)
        verdict = ConsensusVerdict(
            metric_id=metric_id,
            status=INSUFFICIENT,
            consensus=primary[latest],
            detail=f"одне джерело ({spec['primary']}) — незалежної безкоштовної пари немає",
        )
        return latest, verdict, {spec["primary"]: primary[latest]}

    def primary_value(pair: dict, day: date) -> Optional[Decimal]:
        return _diff_at(primary, day) if pair["compare"] == "diff" else primary.get(day)

    common = [
        day for day in sorted(primary, reverse=True)
        if all(day in series and primary_value(pair, day) is not None for pair, series in secondaries)
    ]
    if not common:
        latest = max(primary)
        verdict = ConsensusVerdict(
            metric_id=metric_id,
            status=INSUFFICIENT,
            consensus=primary[latest],
            detail=(
                "немає спільної дати з джерелами звірки ("
                + ", ".join(pair["source"] for pair, _ in secondaries)
                + ") — друге джерело не зібралось або відстає"
            ),
        )
        return latest, verdict, {spec["primary"]: primary[latest]}

    day = common[0]
    # Для "diff"-пар звіряється ЗМІНА; рівень основного ряду тоді не
    # бере участі в медіані (він в інших одиницях — рівень, не приріст).
    compare_kind = secondaries[0][0]["compare"]
    values = {spec["primary"]: primary_value(secondaries[0][0], day)}
    for pair, series in secondaries:
        values[pair["source"]] = series[day]

    verdict = reconcile(
        metric_id,
        [SourceValue(source, value) for source, value in values.items()],
        spec["tolerance_pct"],
        spec.get("abs_tolerance"),
    )
    if compare_kind == "diff":
        verdict.detail = f"звірено тижневу зміну: {verdict.detail}"
        verdict.consensus = primary[day]
    return day, verdict, values


def stale_series(latest_by_metric: dict[str, Optional[date]], today: Optional[date] = None) -> list[str]:
    """Ряди, чия остання точка старша за `stale_business_days` (або
    яких немає зовсім — це теж "не оновлюється"). Подієві ряди
    (`stale_business_days=None`, напр. інтервенції) перевіряються лише
    на наявність: тиша між подіями — норма."""
    stale = []
    for metric_id, latest in latest_by_metric.items():
        threshold = config.FACTOR_SERIES[metric_id]["stale_business_days"]
        if latest is None:
            stale.append(metric_id)
        elif threshold is not None and is_stale(latest, today=today, threshold_business_days=threshold):
            stale.append(metric_id)
    return stale


# --- БД -------------------------------------------------------------

def _fetch_series(conn, source: str, metric_id: str) -> dict[date, Decimal]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT observed_at, value
            FROM v_observations_latest_revision
            WHERE source = %s AND metric_id = %s
            ORDER BY observed_at DESC
            LIMIT %s
            """,
            (source, metric_id, _LOOKBACK_POINTS),
        )
        return {row[0]: Decimal(row[1]) for row in cur.fetchall()}


def _save_verdict(conn, metric_id: str, day: date, verdict: ConsensusVerdict,
                  values: dict[str, Decimal]) -> bool:
    """UPSERT вердикту. True — по цій точці треба надіслати тривогу
    (розбіжність, і про неї ще не повідомляли)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO factor_verdicts
                (metric_id, observed_at, status, consensus, max_spread_pct,
                 source_values, detail, checked_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (metric_id, observed_at) DO UPDATE
               SET status = EXCLUDED.status,
                   consensus = EXCLUDED.consensus,
                   max_spread_pct = EXCLUDED.max_spread_pct,
                   source_values = EXCLUDED.source_values,
                   detail = EXCLUDED.detail,
                   checked_at = now()
            RETURNING notified_at
            """,
            (
                metric_id, day, verdict.status, verdict.consensus, verdict.max_spread_pct,
                json.dumps({s: str(v) for s, v in values.items()}), verdict.detail,
            ),
        )
        (notified_at,) = cur.fetchone()
    conn.commit()
    return notified_at is None and verdict.status not in (AGREE, INSUFFICIENT)


def _mark_notified(conn, keys: list[tuple[str, date]]) -> None:
    with conn.cursor() as cur:
        for metric_id, day in keys:
            cur.execute(
                "UPDATE factor_verdicts SET notified_at = now() WHERE metric_id = %s AND observed_at = %s",
                (metric_id, day),
            )
    conn.commit()


def format_alert(mismatches: list[dict], stale: list[str], latest: dict[str, Optional[date]]) -> str:
    lines: list[str] = []
    if mismatches:
        lines.append("⚠️ <b>Факторні дані: джерела розійшлись</b>")
        lines.append("")
        for item in mismatches:
            values = " | ".join(f"{s}: {v}" for s, v in item["values"].items())
            lines.append(f"• <b>{item['metric_id']}</b> за {item['observed_at']}\n  {values}\n  {item['detail']}")
        lines.append("")
    if stale:
        lines.append("🕒 <b>Факторні ряди застаріли</b> (в аналізі позначені як застарілі)")
        for metric_id in stale:
            when = latest.get(metric_id)
            lines.append(f"• {metric_id}: остання точка {when or 'немає взагалі'}")
        lines.append("")
    lines.append("Сирі дані не змінено (правило 6).")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--notify", action="store_true",
                        help="Telegram при НОВІЙ розбіжності або зміні набору застарілих рядів")
    args = parser.parse_args()

    load_dotenv()
    from common.db import get_connection  # noqa: E402

    conn = get_connection()
    try:
        mismatches: list[dict] = []
        latest_by_metric: dict[str, Optional[date]] = {}

        for metric_id, spec in config.FACTOR_SERIES.items():
            primary = _fetch_series(conn, spec["primary"], metric_id)
            latest_by_metric[metric_id] = max(primary) if primary else None
            secondaries = [
                (pair, _fetch_series(conn, pair["source"], pair["metric_id"]))
                for pair in secondary_pairs(metric_id)
            ]
            result = compare_series(metric_id, spec, primary, secondaries)
            if result is None:
                logger.error("%s: основного ряду (%s) немає в БД — збір не працює?", metric_id, spec["primary"])
                continue

            day, verdict, values = result
            needs_alert = _save_verdict(conn, metric_id, day, verdict, values)
            spread = f" (розкид {verdict.max_spread_pct:.2f}%)" if verdict.max_spread_pct is not None else ""
            logger.info("%s за %s: %s%s — %s", metric_id, day, verdict.status, spread, verdict.detail)
            if needs_alert and spec["alert"]:
                mismatches.append({
                    "metric_id": metric_id, "observed_at": day,
                    "values": values, "detail": verdict.detail,
                })

        stale = stale_series(latest_by_metric)
        for metric_id in stale:
            logger.warning("%s: застарів — остання точка %s", metric_id, latest_by_metric[metric_id])

        if not args.notify:
            return

        from _common import composition_changed, remember_composition, resolve_telegram_credentials  # noqa: E402
        from telegram_client import send_telegram_message  # noqa: E402

        stale_changed = composition_changed(conn, STALE_NOTIFIER, stale)
        report_stale = stale if stale_changed else []
        if not mismatches and not report_stale:
            logger.info("Нових розбіжностей і змін у свіжості немає — сповіщення не надсилається")
            if stale_changed:
                # Набір застарілих зменшився до порожнього — запамʼятати
                # без повідомлення, щоб наступна поява знову була новиною.
                remember_composition(conn, STALE_NOTIFIER, stale)
            return

        token, chat_id = resolve_telegram_credentials()
        send_telegram_message(token, chat_id, format_alert(mismatches, report_stale, latest_by_metric), parse_mode="HTML")
        _mark_notified(conn, [(m["metric_id"], m["observed_at"]) for m in mismatches])
        remember_composition(conn, STALE_NOTIFIER, stale)
        logger.info("Надіслано: розбіжностей %d, застарілих %d", len(mismatches), len(report_stale))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
