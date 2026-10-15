#!/usr/bin/env python3
"""
Звіт про ЯКІСТЬ рішень агента — відсоток влучань за горизонт по кожному
виду списку (`decision_outcomes`, джерело —
`monitoring/evaluate_outcomes.py`). Лише форматування готового
результату, жодної аналітики (reporting/CLAUDE.md).

**Навіщо окреме повідомлення.** Це єдине, що відповідає на питання "а
чи варто взагалі читати решту повідомлень". Решта звітів кажуть, що
агент ДУМАЄ; цей — наскільки він доти був правий.

**Чому тижнево, а не щодня.** Горизонт оцінки 7 днів, тож щоденний
звіт показував би ту саму вибірку з точністю до кількох рядків — і
перетворився б на шум (той самий урок, що звірка FRED↔BLS, яка шле
лише розбіжності).

**Чого тут свідомо немає.** Жодних "варто було купити" чи "сигнал
працює" — лише факт руху проти заявленого напрямку
(analysis/CLAUDE.md, "Заборонені формулювання"). І жодного
домальовування: вибірка менша за `OUTCOME_MIN_SAMPLE` показується як
"замало даних", а не як красивий відсоток.

Використання:
    python outcomes_notify.py
    python outcomes_notify.py --dry-run
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, "data-ingestion"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "monitoring"))

from _common import bold, send_report  # noqa: E402
from common.db import get_connection  # noqa: E402

import config  # noqa: E402
from _outcomes_db import fetch_hit_rates  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Надсилає в Telegram відсоток влучань рішень агента за горизонт "
    "(decision_outcomes) — по видах списків."
)

# Скільки днів оцінок брати у звіт. Тиждень — рівно період між
# прогонами джоби, тож вибірки звітів не перекриваються.
REPORT_WINDOW_DAYS = 7

_KIND_LABELS = {
    "trading_list": "Список торгів",
    "crypto_long": "Крипта LONG",
    "crypto_watch": "Крипта SHORT/WATCH",
    "candidate": "Новачки",
}


def format_report(rows: list[dict], horizon_days: int) -> str:
    lines = [
        f"📊 {bold('Якість рішень')} — горизонт {horizon_days} днів",
        "",
    ]
    anything_scored = False

    for row in rows:
        label = _KIND_LABELS.get(row["decision_kind"], row["decision_kind"])
        scored = (row["correct"] or 0) + (row["incorrect"] or 0)
        lines.append(f"• {bold(label)}: оцінено {row['total']}")

        if scored < config.OUTCOME_MIN_SAMPLE:
            # Чесніше написати "замало", ніж показати 67% на трьох рядках.
            lines.append(f"  замало даних для відсотка ({scored} з напрямком і рухом)")
        else:
            anything_scored = True
            rate = row["correct"] / scored * 100
            avg = row["avg_move"]
            lines.append(
                f"  влучань {rate:.0f}% ({row['correct']}/{scored})"
                + (f", середній рух {float(avg):+.1f}%" if avg is not None else "")
            )

        skipped = []
        if row["flat"]:
            skipped.append(f"{row['flat']} без руху")
        if row["no_claim"]:
            skipped.append(f"{row['no_claim']} без напрямку")
        if row["unavailable"]:
            skipped.append(f"{row['unavailable']} без котирування")
        if skipped:
            lines.append(f"  поза підрахунком: {', '.join(skipped)}")

    lines.append("")
    lines.append(
        f"Влучання вважається при русі понад ±{config.OUTCOME_FLAT_BAND_PCT:g}%; "
        "менший рух і спостереження без напрямку у відсоток не входять."
    )
    if anything_scored:
        lines.append(
            "Одне вікно — це один режим ринку: у падаючому тижні напрямок "
            "«вгору» програє незалежно від якості відбору. Читати варто "
            "динаміку між тижнями, не абсолютне число."
        )
    return "\n".join(lines)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--dry-run", action="store_true", help="показати текст, не надсилати")
    args = parser.parse_args()

    conn = get_connection()
    try:
        rows = fetch_hit_rates(
            conn,
            horizon_days=config.OUTCOME_HORIZON_DAYS,
            since_days=REPORT_WINDOW_DAYS,
        )
    finally:
        conn.close()

    if not rows:
        logger.info("Оцінених рішень за вікно немає — нічого не надсилаю")
        return

    message = format_report(rows, config.OUTCOME_HORIZON_DAYS)
    if args.dry_run:
        print(message)
        return

    send_report(message)
    logger.info("Звіт про якість рішень надіслано (%d видів)", len(rows))


if __name__ == "__main__":
    main()
