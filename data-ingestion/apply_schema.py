#!/usr/bin/env python3
"""
Явне (пере)застосування db/schema.sql до бази.

Навіщо це окремим скриптом, а не тільки через
docker-entrypoint-initdb.d: той механізм виконується ЛИШЕ при першому
створенні volume бази (порожній data dir). Якщо volume вже існував
до зміни schema.sql (напр. додався новий рядок INSERT чи нова
таблиця) — init-скрипт більше не запускається, і зміни треба
застосувати руками. Саме для цього apply_schema.py.

Безпечно запускати повторно: усі CREATE у schema.sql — з IF NOT
EXISTS, а INSERT — з ON CONFLICT DO NOTHING.

Використання (у контейнері app):
    docker compose exec app python data-ingestion/apply_schema.py
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common.db import get_connection  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema.sql"


def main() -> None:
    load_dotenv()

    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        logger.info("Схему застосовано: %s", SCHEMA_PATH)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
