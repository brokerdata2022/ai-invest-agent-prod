#!/usr/bin/env python3
"""
РАЗОВЕ заповнення профілю (`watchlist_assets.asset_class` тощо) для
активів, доданих через Telegram ДО 2026-10-22 — SKYD і подібних.

Початкові активи заповнює сама схема (`db/schema.sql`, значення
перенесено з коду дослівно). Для доданих через бот SQL цього не вміє:
тип треба ПЕРЕВІРИТИ в джерелі, тож тут — ті самі живі запити, що
тепер робить `/watchlist_add` (`common/profile_lookup.py`).

Чіпає лише рядки з `asset_class IS NULL` — повторний запуск нічого не
перетирає. `--dry-run` показує, що буде записано, без запису (запити
до Twelve Data/SEC/Yahoo все одно йдуть: без них нема що показати;
ШІ не викликається взагалі).

Для акції заодно:
- підпис "SKYD" → "Skydance (SKYD)" (лише якщо підпис дорівнював
  тикеру, тобто його ніхто не міняв руками);
- новинний термін GDELT "SKYD" → назва компанії зі статусом 'pending'
  (`revalidate_news_terms` перевірить його сам).

Використання:
    python backfill_watchlist_profile.py --dry-run
    python backfill_watchlist_profile.py
"""

import argparse
import json
import logging
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import asset_profile, profile_lookup  # noqa: E402
from common.db import get_connection  # noqa: E402
from common.watchlist_db import TERM_PENDING, fetch_watchlist, set_profile  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Джерела, клас яких відомий без запиту: ціна з Binance/CoinGecko —
# це крипта (срібло на CoinGecko заповнює схема поіменно).
_CRYPTO_SOURCES = ("binance", "coingecko")


def describe(asset: dict, api_key: str | None) -> dict:
    source, ticker = asset["source"], asset.get("ticker")
    if source in _CRYPTO_SOURCES:
        return {"asset_class": "crypto", "commodity_group": None, "name": None}
    if source == "twelvedata" and ticker:
        return profile_lookup.describe_twelvedata(ticker, api_key)
    # tradingeconomics/yahoo_chart/fred без поіменного рядка в схемі —
    # тип невідомий, і вгадувати його не будемо.
    return {"asset_class": None, "commodity_group": None, "name": None}


def plan(asset: dict, api_key: str | None) -> tuple[dict, dict]:
    """(профіль, інші зміни рядка: label / search_term)."""
    asset_id, ticker = asset["asset_id"], asset.get("ticker") or asset["asset_id"].upper()
    described = describe(asset, api_key)
    primary = asset_profile.chain_entry(asset["source"], asset["metric_id"], asset.get("ticker"))
    profile = profile_lookup.build_profile(
        asset_id, described["asset_class"], described["commodity_group"], ticker,
        described["name"], primary=primary, cik=described.get("cik"),
    )

    extra = {}
    if described["asset_class"] == "stock":
        if (asset.get("label") or "").upper() == ticker.upper():
            extra["label"] = profile_lookup.stock_label("stock", ticker, described["name"])
        clean = asset_profile.clean_name(described["name"])
        if clean and asset.get("search_term") in (None, f'"{ticker}"'):
            extra["search_term"] = f'"{clean}"'
    return profile, extra


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="показати, без запису")
    args = parser.parse_args()
    load_dotenv()
    api_key = os.environ.get("TWELVEDATA_API_KEY")

    conn = get_connection()
    try:
        todo = [a for a in fetch_watchlist(conn, enabled_only=False) if a.get("asset_class") is None]
        logger.info("Активів без профілю: %d (%s)", len(todo), ", ".join(a["asset_id"] for a in todo) or "—")
        for asset in todo:
            profile, extra = plan(asset, api_key)
            print(f"\n{asset['asset_id']}: {json.dumps(profile, ensure_ascii=False)}")
            if extra:
                print(f"  + {json.dumps(extra, ensure_ascii=False)}")
            if profile["asset_class"] is None:
                logger.warning("%s: тип не визначено — лишається без класу (видно в /watchlist_list)", asset["asset_id"])
            if args.dry_run:
                continue
            set_profile(conn, asset["asset_id"], profile)
            with conn.cursor() as cur:
                if "label" in extra:
                    cur.execute("UPDATE watchlist_assets SET label = %s WHERE asset_id = %s",
                                (extra["label"], asset["asset_id"]))
                if "search_term" in extra:
                    cur.execute(
                        "UPDATE watchlist_assets SET search_term = %s, search_term_status = %s WHERE asset_id = %s",
                        (extra["search_term"], TERM_PENDING, asset["asset_id"]),
                    )
            conn.commit()
    finally:
        conn.close()
    if args.dry_run:
        print("\n(--dry-run: нічого не записано)")


if __name__ == "__main__":
    main()
