#!/usr/bin/env python3
"""
Перезбір УСІХ відомих показників за один запуск — зручно після
`docker compose down -v` (порожня БД) або першого розгортання, коли
руками ганяти run_collect.py --metric X по одному довго.

На відміну від run_collect.py (одна метрика, для дебагу), тут
навмисно "best effort": помилка одного metric_id (напр. немає
FRED_API_KEY, чи мережева проблема) не зупиняє решту — в кінці
друкується підсумок, що зібралось, а що ні.

Використання:
    python collect_all.py
    python collect_all.py --limit 20
    python collect_all.py --source fred        # тільки FRED
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.db import get_connection, insert_observations  # noqa: E402
from run_collect import ADAPTERS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", choices=sorted(ADAPTERS), default=None,
        help="зібрати тільки метрики цього джерела (за замовчуванням — усі)",
    )
    parser.add_argument("--limit", type=int, default=10, help="скільки останніх спостережень на метрику")
    args = parser.parse_args()

    sources = [args.source] if args.source else sorted(ADAPTERS)

    conn = get_connection()
    ok: list[str] = []
    failed: list[tuple[str, str]] = []

    try:
        for source in sources:
            cfg = ADAPTERS[source]
            api_key = None
            if cfg["needs_api_key"]:
                api_key = os.environ.get(cfg["needs_api_key"])
                if not api_key:
                    for metric_id in cfg["metrics"]:
                        failed.append((metric_id, f"{cfg['needs_api_key']} не задано в .env"))
                    continue

            for metric_id in sorted(cfg["metrics"]):
                try:
                    adapter = cfg["class"](api_key=api_key, metric_id=metric_id) \
                        if api_key else cfg["class"](metric_id=metric_id)
                    records = adapter.collect(limit=args.limit)
                    if not records:
                        failed.append((metric_id, "0 записів (перевірте вручну)"))
                        continue
                    inserted = insert_observations(conn, records)
                    logger.info("%s/%s: %d записів (%d нових/змінених)", source, metric_id, len(records), inserted)
                    ok.append(metric_id)
                except Exception as exc:  # noqa: BLE001 — навмисно широкий catch, best-effort цикл
                    logger.error("%s/%s провалилось: %s", source, metric_id, exc)
                    failed.append((metric_id, str(exc)))
    finally:
        conn.close()

    print("\n--- Підсумок ---")
    print(f"Успішно: {len(ok)} — {', '.join(ok) if ok else '(нічого)'}")
    if failed:
        print(f"Провалилось: {len(failed)}")
        for metric_id, reason in failed:
            print(f"  - {metric_id}: {reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
