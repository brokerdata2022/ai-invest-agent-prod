#!/usr/bin/env python3
"""
Реєструє "/"-меню команд бота в Telegram (setMyCommands) — назва+опис
кожної джоби (command_descriptions.py:JOB_DESCRIPTIONS) видно одразу
при наборі "/" у чаті, без окремого /help-запиту.

Ручний разовий запуск (НЕ в розкладі orchestration/schedule.py) —
список команд змінюється рідко (новий job), не варто бити Telegram API
щохвилини заради того, що майже завжди вже не змінилось. Перезапустити
після додавання/перейменування джоби в jobs.py.

Використання:
    python register_telegram_commands.py
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "reporting"))

from command_descriptions import INFO_JOB_NAMES, JOB_DESCRIPTIONS, SPECIAL_COMMAND_DESCRIPTIONS  # noqa: E402
from telegram_client import set_my_commands  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def build_commands_payload() -> list[dict]:
    """Сортуємо за назвою — стабільний, передбачуваний порядок у меню
    Telegram (а не порядок вставки в словнику). Лише INFO_JOB_NAMES
    (2026-10-03, рішення користувача: "забери технічні джоби з меню")
    + watchlist-команди (SPECIAL_COMMAND_DESCRIPTIONS, своя гілка
    диспетчеризації в telegram_commands.py, не JOBS)."""
    info_descriptions = {k: v for k, v in JOB_DESCRIPTIONS.items() if k in INFO_JOB_NAMES}
    all_descriptions = {**info_descriptions, **SPECIAL_COMMAND_DESCRIPTIONS}
    return [
        {"command": name, "description": all_descriptions[name]}
        for name in sorted(all_descriptions)
    ]


def main() -> None:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN не задано в .env")
        sys.exit(1)

    commands = build_commands_payload()
    set_my_commands(token, commands)
    logger.info("Зареєстровано %d команд у Telegram-меню", len(commands))


if __name__ == "__main__":
    main()
