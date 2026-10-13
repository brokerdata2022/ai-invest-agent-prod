#!/usr/bin/env python3
"""
Ручний запуск збору офіційних RSS-фідів (news/rss_feeds.py) →
raw_news. Окремо від run_collect_news.py (GDELT) — інший механізм
джерела (RSS, не пошуковий query), той самий вихід (raw_news,
спільна схема/дедуп по source+external_id).

Використання:
    python run_collect_rss.py            # усі фіди з реєстру
    python run_collect_rss.py --feed fed_rss
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.db import get_connection  # noqa: E402
from common.news_db import insert_news_batch  # noqa: E402
from news.rss_adapter import RssAdapter  # noqa: E402
from news.rss_feeds import RSS_FEEDS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", choices=sorted(RSS_FEEDS), default=None)
    args = parser.parse_args()

    feeds = {args.feed: RSS_FEEDS[args.feed]} if args.feed else RSS_FEEDS

    conn = get_connection()
    total_inserted = 0
    try:
        for name, cfg in feeds.items():
            adapter = RssAdapter(source=name, stream=cfg["stream"], feed_url=cfg["url"])
            try:
                records = adapter.collect()
            except requests.exceptions.RequestException:
                # Один недоступний фід не повинен зривати збір решти
                # (той самий принцип, що й у collect_stock_news.py) —
                # пропускаємо, наступний запуск підхопить.
                logger.exception("Пропускаю фід %s: мережева помилка", name)
                continue

            logger.info("%s: отримано %d записів", name, len(records))
            if records:
                total_inserted += insert_news_batch(conn, records)
    finally:
        conn.close()

    logger.info("Готово: %d нових записів записано в raw_news", total_inserted)


if __name__ == "__main__":
    main()
