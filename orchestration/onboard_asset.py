#!/usr/bin/env python3
"""
Онбординг активу, щойно доданого до списку обраних: усі дані, які
агент має про актив такого класу, — ОДРАЗУ, а не з наступним прогоном
кожної джоби за розкладом.

**Чому (2026-10-22, живий кейс SKYD).** Акцію додано через Telegram, і
чек-лист вийшов лише з макро й новин: звітність SEC збиралась лише для
S&P 500 (щонеділі), тексти 10-K/10-Q — лише для топу скринінгу
(щосуботи), а чек-лист складався в ту саму секунду, коли даних
компанії в базі ще не було. Тепер порядок такий, щоб чек-лист бачив
усе, що для активу вже зібрано:

1. ціни з ГЛИБОКОЮ історією по всьому ланцюжку джерел
   (`collect_watchlist.py --include-hourly`): тренд-шар вимагає 60
   точок, а щоденний збір бере 10;
2. лише акції: фундаментал SEC EDGAR (`run_collect.py --source sec_edgar`);
3. лише акції: 8-K, Form 4, консенсус аналітиків і free float
   (`collect_company_data.py`);
4. лише акції: текст останнього 10-K/10-Q і тези з нього
   (`analyze_filings.py`);
5. чек-лист чинників (`build_checklist.py --notify`).

Крок, що впав, не зупиняє наступні: ціна без звітності корисніша, ніж
нічого. Логіки тут немає (orchestration/CLAUDE.md) — лише порядок
викликів уже наявних скриптів.

`--dry-run` — без жодного виклику ШІ (дев: токени не витрачаються):
тези зі звіту не витягуються (`--collect-only`), чек-лист лише
друкує каталог і промпт.

Використання:
    python onboard_asset.py --asset skyd --dry-run
    python onboard_asset.py --asset skyd --notify
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "data-ingestion"))

from common.db import get_connection  # noqa: E402
from common.watchlist_db import find_asset  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Глибина історії цін при онбордингу — та сама, що Yahoo дає завжди
# (`collect_watchlist.py:_YAHOO_MIN_POINTS`), щоб тренд-шар
# (`config.TREND_LONG_DAYS`=60) мав опору з першого дня.
_HISTORY_POINTS = "130"
_STEP_TIMEOUT_SECONDS = 1800


def plan_steps(asset: dict, dry_run: bool, notify: bool) -> list[tuple[str, list[str]]]:
    """(назва кроку, команда) у порядку виконання — чиста функція."""
    py = sys.executable
    asset_id = asset["asset_id"]
    steps = [(
        "ціни",
        [py, str(REPO_ROOT / "data-ingestion" / "collect_watchlist.py"),
         "--asset", asset_id, "--limit", _HISTORY_POINTS, "--include-hourly"],
    )]
    ticker = asset.get("ticker")
    if asset.get("asset_class") == "stock" and ticker:
        steps.append((
            "звітність SEC",
            [py, str(REPO_ROOT / "data-ingestion" / "run_collect.py"),
             "--source", "sec_edgar", "--ticker", ticker],
        ))
        steps.append((
            "події, інсайдери, аналітики",
            [py, str(REPO_ROOT / "data-ingestion" / "collect_company_data.py"), "--ticker", ticker],
        ))
        filings = [py, str(REPO_ROOT / "analysis" / "fundamental" / "analyze_filings.py"), "--ticker", ticker]
        if dry_run:
            filings.append("--collect-only")
        steps.append(("тексти 10-K/10-Q", filings))
    checklist = [py, str(REPO_ROOT / "analysis" / "fundamental" / "build_checklist.py"), "--asset", asset_id]
    if dry_run:
        checklist.append("--dry-run")
    elif notify:
        checklist.append("--notify")
    steps.append(("чек-лист", checklist))
    return steps


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--asset", required=True)
    parser.add_argument("--dry-run", action="store_true", help="без викликів ШІ")
    parser.add_argument("--notify", action="store_true", help="надіслати чек-лист у Telegram")
    args = parser.parse_args()
    load_dotenv()

    conn = get_connection()
    try:
        asset = find_asset(conn, args.asset)
    finally:
        conn.close()
    if asset is None or not asset["enabled"]:
        sys.exit(f"{args.asset}: немає серед увімкнених активів")

    failed = []
    for name, command in plan_steps(asset, args.dry_run, args.notify):
        logger.info("%s: крок «%s»", args.asset, name)
        try:
            result = subprocess.run(command, cwd=REPO_ROOT, timeout=_STEP_TIMEOUT_SECONDS)
            ok = result.returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
        if not ok:
            logger.error("%s: крок «%s» провалився — далі без нього", args.asset, name)
            failed.append(name)

    if failed:
        logger.warning("%s: онбординг завершено з пропусками: %s", args.asset, ", ".join(failed))
        sys.exit(1)
    logger.info("%s: онбординг завершено", args.asset)


if __name__ == "__main__":
    main()
