"""
Telegram-алерти рівня оркестрації (провал джоби, простій планувальника).
Використовує спільний reporting/telegram_client.py (той самий хелпер,
що telegram_notify.py/news_notify.py) — не дублює HTTP-виклик утретє.
"""

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "reporting"))
from telegram_client import send_telegram_message  # noqa: E402

logger = logging.getLogger(__name__)

_MAX_LOG_CHARS = 1500


def notify_failure(job_name: str, reason: str, log_output: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.error(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не задані — алерт про провал %s не надіслано",
            job_name,
        )
        return

    tail = log_output.strip()[-_MAX_LOG_CHARS:] if log_output and log_output.strip() else "(немає виводу)"
    text = f"⚠️ Джоба провалилась: {job_name}\n{reason}\n\n{tail}"
    try:
        send_telegram_message(token, chat_id, text)
    except Exception:
        logger.error("Не вдалось надіслати Telegram-алерт про провал %s", job_name, exc_info=True)


def notify_scheduler_gap(last_seen: datetime, resumed_at: datetime, gap: timedelta) -> None:
    """Один раз при старті процесу (main.py:check_startup_gap), якщо
    розрив з останнім heartbeat-тиком (jobs.py:_scheduler_heartbeat)
    перевищив поріг — пояснює користувачу ЗАЗДАЛЕГІДЬ (а не після
    здивування "чому звіт так пізно"), що джерело затримки — простій
    самого планувальника, а не повільний конвеєр (живий випадок
    2026-10-02, docs/decisions.md P0)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.error(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не задані — алерт про простій планувальника не надіслано"
        )
        return

    hours, remainder = divmod(int(gap.total_seconds()), 3600)
    minutes = remainder // 60
    text = (
        f"⏰ Планувальник був недоступний {hours}г {minutes}хв "
        f"(з {last_seen:%Y-%m-%d %H:%M} до {resumed_at:%Y-%m-%d %H:%M} UTC).\n"
        "Релізи/новини, що вийшли за цей час, оброблені вже після "
        "відновлення — тому могли прийти з запізненням."
    )
    try:
        send_telegram_message(token, chat_id, text)
    except Exception:
        logger.error("Не вдалось надіслати Telegram-алерт про простій планувальника", exc_info=True)
