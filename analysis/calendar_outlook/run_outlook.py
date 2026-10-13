#!/usr/bin/env python3
"""
Ранковий огляд календаря релізів (docs/decisions.md 2026-10-03,
рішення користувача): у понеділок — ОБИДВА огляди (на весь тиждень і
окремо на сьогодні), у кожен інший робочий день — лише на сьогодні.
Читає вже заведені 'pending'-рядки release_log
(monitoring/refresh_calendar.py заводить їх наперед), LLM лише коротко
інтерпретує перелік (synthesize.py).

Ідемпотентно на рівні (outlook_date, scope): якщо конкретний огляд уже
збережено (UNIQUE outlook_date+scope), повторний прогін (ретрай
orchestration/runner.py) пропускає саме його LLM-виклик, а не дублює —
інший scope того самого дня (якщо ще не збережений) все одно
генерується.

Використання:
    python run_outlook.py
"""

import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ANALYSIS_DIR)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))

from common.db import get_connection  # noqa: E402
from llm_common import LlmError, SynthesisResult, log_llm_call, require_api_key  # noqa: E402

from calendar_outlook._db import fetch_upcoming, has_outlook, save_outlook  # noqa: E402
from calendar_outlook.synthesize import synthesize_outlook  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

NO_EVENTS_SUMMARY = "Запланованих релізів немає — спокійний день без суттєвих макро-подій."


def determine_scope(today: date) -> list[tuple[str, date, date]]:
    """Чиста логіка (без БД/LLM): понеділок (weekday()==0) →
    [('week', пн, пт), ('day', сьогодні, сьогодні)] — ОБИДВА огляди
    (рішення користувача 2026-10-03: "в понеділок має бути два
    календарі — на весь тиждень і з аналізом на день", не тиждень
    ЗАМІСТЬ дня). Вівторок-п'ятниця → лише [('day', сьогодні,
    сьогодні)]. Вихідні → [] (нічого не генеруємо; schedule.py й так
    запускає джобу лише mon-fri, тут — захист для ручного запуску)."""
    weekday = today.weekday()
    if weekday == 0:
        return [("week", today, today + timedelta(days=4)), ("day", today, today)]
    if weekday in (1, 2, 3, 4):
        return [("day", today, today)]
    return []


def _generate_outlook(conn, today: date, scope: str, start: date, end: date) -> int | None:
    """Один конкретний огляд (одного scope) — ідемпотентний виклик, що
    повертає id нового рядка calendar_outlook, або None якщо він уже
    існує чи LLM-синтез провалився."""
    if has_outlook(conn, outlook_date=today, scope=scope):
        logger.info("Огляд на %s (%s) уже існує — пропущено", today, scope)
        return None

    entries = fetch_upcoming(conn, start, end)
    logger.info("%s: %d запланованих релізів у діапазоні %s..%s", scope, len(entries), start, end)

    if not entries:
        result = SynthesisResult(
            direction="neutral", confidence=1.0, summary=NO_EVENTS_SUMMARY,
            reasoning="Немає pending-рядків release_log у діапазоні.",
        )
        llm_call_id = None
    else:
        try:
            result, prompt, llm = synthesize_outlook(entries, scope)
        except Exception:
            logger.exception("Помилка LLM-синтезу огляду календаря (%s) — пропущено", scope)
            return None
        llm_call_id = log_llm_call(
            conn, llm=llm, purpose="calendar_outlook",
            prompt=prompt, source_ref=f"{today}:{scope}",
        )

    outlook_id = save_outlook(
        conn, outlook_date=today, scope=scope,
        release_log_ids=[e["id"] for e in entries], result=result, llm_call_id=llm_call_id,
    )
    if outlook_id is not None:
        logger.info("Збережено огляд id=%s (%s, %s): %s", outlook_id, today, scope, result.summary)
    return outlook_id


def run_outlook(conn, today: date) -> list[int]:
    """Генерує всі огляди, належні цьому дню (determine_scope) — у
    понеділок це два окремі рядки calendar_outlook (week + day), в
    інший робочий день один. Повертає id усіх НОВО створених рядків
    (пропущені через ідемпотентність — не в списку)."""
    scopes = determine_scope(today)
    if not scopes:
        logger.info("Вихідний день (%s) — огляд календаря не генерується", today)
        return []

    outlook_ids = []
    for scope, start, end in scopes:
        outlook_id = _generate_outlook(conn, today, scope, start, end)
        if outlook_id is not None:
            outlook_ids.append(outlook_id)
    return outlook_ids


def main() -> None:
    load_dotenv()
    require_api_key("calendar_outlook")

    conn = get_connection()
    try:
        today = datetime.now(timezone.utc).date()
        run_outlook(conn, today)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
