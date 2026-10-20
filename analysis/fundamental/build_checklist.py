#!/usr/bin/env python3
"""
Скласти (чи показати) чек-лист чинників для активів списку обраних
(`fundamental/checklist.py`).

Зазвичай окремо запускати не треба: фундаментальний аналіз сам складає
чек-лист активу, у якого його ще немає або каталог даних змінився. Цей
скрипт — для двох випадків:
- одразу після `/watchlist_add` (бот запускає його з `--notify`), щоб
  користувач одразу бачив, ЗА ЧИМ агент тепер аналізуватиме новий
  актив, а не дізнавався про це наступного дня;
- для живої перевірки й ручного перескладання (`--force`).

Використання:
    python build_checklist.py                     # усі активи без ручного чек-листа
    python build_checklist.py --asset bnb
    python build_checklist.py --asset bnb --dry-run   # каталог і промпт, без ШІ
    python build_checklist.py --asset bnb --force     # скласти наново
    python build_checklist.py --asset bnb --notify    # + надіслати в Telegram
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_ANALYSIS_DIR)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, "data-ingestion"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "reporting"))
sys.path.insert(0, _ANALYSIS_DIR)

import config  # noqa: E402
from common.db import get_connection  # noqa: E402
from fundamental._db import fetch_watchlist_assets  # noqa: E402
from fundamental.checklist import (  # noqa: E402
    build_catalog,
    build_prompt,
    format_checklist,
    resolve_checklist,
)
from llm_common import require_api_key  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--asset", default=None, help="один актив (asset_id)")
    parser.add_argument("--force", action="store_true", help="скласти наново, навіть якщо каталог не змінився")
    parser.add_argument("--dry-run", action="store_true", help="показати каталог і промпт, без виклику ШІ")
    parser.add_argument("--notify", action="store_true", help="надіслати чек-лист у Telegram")
    args = parser.parse_args()

    load_dotenv()
    if not args.dry_run:
        require_api_key("asset_checklist")

    conn = get_connection()
    try:
        watchlist = fetch_watchlist_assets(conn)
        if args.asset:
            assets = [a for a in watchlist if a["asset_id"] == args.asset]
            if not assets:
                logger.error("%s: немає серед увімкнених активів watchlist", args.asset)
                sys.exit(1)
        else:
            assets = [a for a in watchlist if a["asset_id"] not in config.ASSET_FACTORS]
            logger.info("Активів без ручного чек-листа: %d", len(assets))

        for asset in assets:
            if args.dry_run:
                catalog = build_catalog(conn, asset, watchlist)
                print(f"\n===== {asset['asset_id']}: каталог {len(catalog)} рядів =====")
                print(build_prompt(asset, catalog))
                continue

            checklist = resolve_checklist(conn, asset, watchlist, force=args.force)
            text = format_checklist(asset, checklist)
            print(f"\n===== {asset['asset_id']} ({checklist.origin}) =====")
            for key, label in checklist.specs:
                print(f"  {key:45} {label}")
            if checklist.gaps:
                print("  прогалини: " + "; ".join(checklist.gaps))
            if checklist.rejected:
                print("  відкинуто (немає в каталозі): " + ", ".join(checklist.rejected))

            if args.notify:
                from _common import resolve_telegram_credentials  # noqa: E402
                from telegram_client import send_telegram_message  # noqa: E402

                token, chat_id = resolve_telegram_credentials()
                send_telegram_message(token, chat_id, text, parse_mode="HTML")
                logger.info("%s: чек-лист надіслано в Telegram", asset["asset_id"])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
