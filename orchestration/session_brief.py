#!/usr/bin/env python3
"""
Сесійний бриф — ДО відкриття біржі й одразу в Telegram (2026-10-22,
пункт 5 плану сесії).

**Чому.** Було: синтез через +20 хв ПІСЛЯ відкриття, а повідомлення —
окремою джобою ще пізніше (США — 17:40 за Києвом, понад годину після
відкриття); список для торгів жив окремим повідомленням за своїм
розкладом. Тепер ОДИН процес за `config.SESSION_PREOPEN_MINUTES` до
відкриття (розклад — `schedule.py`, у поясі біржі):

1. свіжий список для торгів (`run_trading_list.py`) — щоб бриф
   показував активи з подією саме на цю сесію;
2. синтез стану ринку на цю сесію (`synthesize_market.py --session`);
3. надсилання одразу (`market_notify.py`): синтез + активи для торгів
   у цю сесію, або прямо "немає".

Логіки тут немає (orchestration/CLAUDE.md) — лише порядок. Крок, що
впав, зупиняє наступні: бриф без свіжого списку або без синтезу був би
неповним, а `notify_market_synthesis` за розкладом лишається
страховкою для вже готового синтезу.

`--dry-run` — без ШІ й без Telegram (дев): список рахується без запису,
синтез друкує промпт, повідомлення друкується.

Використання:
    python session_brief.py --session us --dry-run
    python session_brief.py --session us
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SESSIONS = ("asia", "europe", "us")
_STEP_TIMEOUT_SECONDS = 1200


def plan_steps(session: str, dry_run: bool) -> list[tuple[str, list[str]]]:
    py = sys.executable
    trading = [py, str(REPO_ROOT / "analysis" / "trading_list" / "run_trading_list.py")]
    synthesis = [py, str(REPO_ROOT / "analysis" / "news_analysis" / "synthesize_market.py"), "--session", session]
    notify = [py, str(REPO_ROOT / "reporting" / "market_notify.py"), "--session", session]
    if dry_run:
        trading.append("--dry-run")
        synthesis.append("--dry-run")
        notify.append("--dry-run")
    return [("список для торгів", trading), ("синтез сесії", synthesis), ("надсилання", notify)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", choices=SESSIONS, required=True)
    parser.add_argument("--dry-run", action="store_true", help="без ШІ, без запису й без Telegram")
    args = parser.parse_args()

    for name, command in plan_steps(args.session, args.dry_run):
        logger.info("Бриф %s: крок «%s»", args.session, name)
        try:
            result = subprocess.run(command, cwd=REPO_ROOT, timeout=_STEP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            sys.exit(f"Бриф {args.session}: крок «{name}» не вклався в {_STEP_TIMEOUT_SECONDS} с")
        if result.returncode != 0:
            sys.exit(f"Бриф {args.session}: крок «{name}» провалився (код {result.returncode})")
    logger.info("Бриф %s: готово", args.session)


if __name__ == "__main__":
    main()
