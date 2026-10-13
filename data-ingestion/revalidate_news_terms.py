#!/usr/bin/env python3
"""
Доводить до кінця перевірку новинних термінів watchlist-активів, яку
не вдалось провести при додаванні (`search_term_status = 'pending'`).

## Чому ця джоба існує

Живий фідбек користувача 2026-10-06: "я тут в запиті не бачу активів,
які були добавлені в список обраних через телеграм". Причина — не в
конкретному активі, а в тому, що ДВА РІЗНІ результати live-перевірки
GDELT зберігались однаково (`search_term = NULL`):
1. GDELT ВІДМОВИВ у терміні (занадто коротка/загальна фраза) —
   остаточно, повторювати безглуздо;
2. перевірку НЕ ВДАЛОСЬ провести (429 / мʼякий троттл / мережа) —
   тимчасово, і мусить бути повторено.

З баном GDELT (2026-10-05) другий випадок став постійним: кожен новий
актив, доданий через Telegram, назавжди лишався б без новин, мовчки.
Тепер такий термін зберігається як 'pending', а ця джоба щодня
пробує знову — і система сама себе лікує, коли джерело оживає
(critical rule 7 кореневого CLAUDE.md: контроль коректності —
властивість системи, не ручна робота в сесії).

Використання:
    python revalidate_news_terms.py
    python revalidate_news_terms.py --dry-run
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.db import get_connection  # noqa: E402
from common.rate_limit import make_pacer  # noqa: E402
from common.watchlist_db import (  # noqa: E402
    TERM_REJECTED,
    TERM_VALIDATED,
    fetch_pending_terms,
    set_term_status,
)
from news.gdelt_adapter import validate_gdelt_term  # noqa: E402

from config import (  # noqa: E402
    GDELT_MIN_REQUEST_INTERVAL_SECONDS,
    GDELT_THROTTLE_MAX_WAIT_SECONDS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="перевірити й показати, не писати")
    args = parser.parse_args()

    conn = get_connection()
    try:
        pending = fetch_pending_terms(conn)
        if not pending:
            logger.info("Немає термінів, що очікують перевірки — нічого робити")
            return

        logger.info("Очікують перевірки: %d", len(pending))
        # Той самий глобальний обмежувач, що збір новин — інакше ця
        # джоба сама вганяла б IP у штрафний стан (docs/decisions.md,
        # 2026-10-05).
        pacer = make_pacer(
            conn,
            api="gdelt",
            min_interval_seconds=GDELT_MIN_REQUEST_INTERVAL_SECONDS,
            max_wait_seconds=GDELT_THROTTLE_MAX_WAIT_SECONDS,
        )

        promoted = rejected = still_pending = 0
        for asset in pending:
            # Термін у БД лежить уже в лапках — `validate_gdelt_term`
            # квотує сам, тож лапки знімаються.
            term = asset["search_term"].strip('"')
            pacer()
            verdict = validate_gdelt_term(term)

            if verdict is True:
                status, promoted = TERM_VALIDATED, promoted + 1
            elif verdict is False:
                status, rejected = TERM_REJECTED, rejected + 1
            else:
                still_pending += 1
                logger.info("%s (%r): GDELT і далі недоступний — лишається pending",
                            asset["asset_id"], term)
                continue

            if args.dry_run:
                logger.info("--dry-run: %s (%r) -> %s", asset["asset_id"], term, status)
                continue
            set_term_status(conn, asset["asset_id"], status)
            logger.info("%s (%r): %s", asset["asset_id"], term, status)

        logger.info(
            "Готово: %d підтверджено, %d відхилено, %d лишилось на потім",
            promoted, rejected, still_pending,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
