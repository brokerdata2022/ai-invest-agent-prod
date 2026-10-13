#!/usr/bin/env python3
"""
Релізний конвеєр ОДНИМ процесом замість семи щохвилинних джоб.

**Навіщо (2026-10-13).** Сім джоб ходили щохвилини:
`check_releases` → `update_forecasts` → `compare_expectations` →
`synthesize_expectations` → `notify_expectations` /
`notify_release_impact` / `notify_forecasts`. Це 7 × 1440 = **10 080
запусків Python-інтерпретера на добу**, з яких ~99.9% закінчувались
"нічого робити": релізів у нас ~10 за 15 днів. Кожен запуск — це
окремий процес, окремий конект до БД і окремий рядок у логах.

**Що саме змінюється, а що НІ.**
- Змінюється: спершу ОДИН дешевий SQL-запит "чи є взагалі робота"
  (`has_work()`), і лише якщо є — виконується ланцюжок. На тихій
  хвилині процес завершується за десятки мілісекунд, не породжуючи
  семи дочірніх.
- НЕ змінюється: самі кроки, їхній код, їхній ПОРЯДОК і контракт
  провалу. Кожен крок і далі окремий subprocess із власним
  exit-кодом, тому `runner.py`-ретраї та алерти працюють як раніше.

**ПОРЯДОК КРИТИЧНИЙ і закріплений тестом.** `update_forecasts` мусить
іти ПЕРЕД `compare_expectations`: обидва читають рядки
`release_log` у статусі 'detected', але власником переходу в
'processed' є `compare_releases.py`. Якщо порядок переставити,
'detected'-рядок стане 'processed' раніше, ніж прогноз його побачить,
і прогноз НЕ ОНОВИТЬСЯ ніколи — це не самолікується наступного тику,
на відміну від решти пар (orchestration/CLAUDE.md, docs/decisions.md
2026-10-02).

**Чому крок, що впав, НЕ зупиняє наступні.** Збій `synthesize_
expectations` (виклик LLM — найненадійніший крок ланцюжка) не повинен
забирати `notify_forecasts`, якому потрібні зовсім інші дані. Тому
кроки виконуються всі, а ненульовий exit повертається В КІНЦІ, якщо
хоч один упав — тоді `runner.py` ретраїть увесь конвеєр, і вже
виконані кроки просто не знайдуть роботи (усі вони ідемпотентні за
`notified_at`/статусом).
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "data-ingestion"))

from common.db import get_connection  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Кроки В ПОРЯДКУ ВИКОНАННЯ. Порядок — частина контракту, не стиль
# (див. докстрінг модуля); `tests/test_release_pipeline.py` його
# стереже.
STEPS: tuple[tuple[str, Path], ...] = (
    ("check_releases", REPO_ROOT / "monitoring" / "check_releases.py"),
    ("update_forecasts", REPO_ROOT / "analysis" / "forecasting" / "update_forecasts.py"),
    ("compare_expectations", REPO_ROOT / "analysis" / "expectations" / "compare_releases.py"),
    ("synthesize_expectations", REPO_ROOT / "analysis" / "expectations" / "synthesize.py"),
    ("notify_expectations", REPO_ROOT / "reporting" / "expectations_notify.py"),
    ("notify_release_impact", REPO_ROOT / "reporting" / "release_impact_notify.py"),
    ("notify_forecasts", REPO_ROOT / "reporting" / "forecast_notify.py"),
)

# Один запит замість семи процесів. Кожна частина відповідає тому, що
# шукає відповідний крок:
#   1. pending-реліз, чий час уже настав      → check_releases
#   2. рядок у статусі 'detected'             → update_forecasts,
#                                                compare_expectations
#   3. ненадіслане порівняння факт/очікування → notify_expectations
#   4. ненадісланий розбір впливу             → notify_release_impact
#   5. ненадісланий прогноз                   → notify_forecasts
#
# `EXISTS`-підзапити, не COUNT: потрібен лише ФАКТ наявності, і
# планувальник зупиняється на першому знайденому рядку.
#
# `expectation_synthesis` має ВЛАСНИЙ дедуп-стовпець
# `impact_notified_at` (не `expectation_comparisons.notified_at`, яким
# керує expectations_notify.py — два звіти незалежно відстежують, що
# вже надіслали). Перша версія цього запиту брала "синтез за останню
# добу" як обережне наближення, і це означало б, що після ОДНОГО
# релізу конвеєр вважає роботу наявною цілу добу — тобто 1440
# холостих прогонів замість нуля. Точний стовпець прибирає це.
WORK_QUERY = """
SELECT
    EXISTS (
        SELECT 1 FROM release_log
        WHERE status = 'pending' AND scheduled_at <= now()
    ) AS pending_due,
    EXISTS (
        SELECT 1 FROM release_log WHERE status = 'detected'
    ) AS detected,
    -- Фільтр impact_level ДУБЛЮЄ `expectations_notify.py:
    -- DEFAULT_IMPACT_LEVELS` СВІДОМО. Без нього гейт назавжди
    -- залишається True: рядок із impact='low' той скрипт ніколи не
    -- надішле й ніколи не позначить, тож "ненадісланих порівнянь"
    -- технічно завжди є хоч одне (живий стан 13 жовтня: id=3,
    -- impact='low', висить із 1 жовтня). Умови мусять лишатись
    -- синхронними — за цим стежить tests/test_release_pipeline.py.
    EXISTS (
        SELECT 1 FROM expectation_comparisons ec
        JOIN release_log rl ON rl.id = ec.release_log_id
        WHERE ec.notified_at IS NULL
          AND rl.impact_level IN ('high', 'medium')
    ) AS unnotified_comparisons,
    EXISTS (
    -- Та сама синхронізація, що вище: `release_impact_notify.py`
    -- надсилає лише синтези з НЕПОРОЖНІМ `asset_impacts`.
        SELECT 1 FROM expectation_synthesis
        WHERE impact_notified_at IS NULL
          AND asset_impacts IS NOT NULL
          AND jsonb_array_length(asset_impacts) > 0
    ) AS unnotified_impacts,
    -- Два фільтри `forecast_notify.py`, обидва обов'язкові для гейта:
    --   * `naive_*` він не надсилає (повідомлення сказало б "без
    --     змін" і більше нічого) — позначає сам, але між створенням і
    --     позначенням гейт інакше бачив би роботу;
    --   * `linear_trend` виключений назовсім (`EXCLUDED_METHOD`) —
    --     це стара БАЗОВА ЛІНІЯ, не бойова модель. Живий стан 13
    --     жовтня: рядок id=1 від 27 вересня висить ненадісланим
    --     назавжди, і без цього фільтра гейт був би True вічно.
    EXISTS (
        SELECT 1 FROM metric_forecasts
        WHERE notified_at IS NULL
          AND method NOT LIKE 'naive%%'
          AND method <> 'linear_trend'
    ) AS unnotified_forecasts
"""


def has_work(conn) -> tuple[bool, dict]:
    """(чи є робота, по-компонентна розкладка для логу)."""
    with conn.cursor() as cur:
        cur.execute(WORK_QUERY)
        columns = [c[0] for c in cur.description]
        row = dict(zip(columns, cur.fetchone()))
    return any(row.values()), row


def run_step(name: str, script: Path) -> bool:
    """True, якщо крок завершився успішно."""
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=1800,
        cwd=str(REPO_ROOT), env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    output = (result.stdout or "") + (result.stderr or "")
    if output.strip():
        # Вивід кроку йде в наш stdout — тобто в той самий файл логу,
        # що й раніше писав runner.py, лише один на весь конвеєр.
        print(f"--- {name} ---\n{output.rstrip()}")
    if result.returncode != 0:
        logger.error("%s: exit code %d", name, result.returncode)
        return False
    return True


def main() -> None:
    conn = get_connection()
    try:
        work, breakdown = has_work(conn)
    finally:
        conn.close()

    if not work:
        # Тиха хвилина — найчастіший випадок (релізів ~10 за 15 днів).
        # Один рядок логу замість семи процесів.
        logger.info("Релізної роботи немає — конвеєр не запускається")
        return

    logger.info(
        "Є робота: %s",
        ", ".join(name for name, value in breakdown.items() if value) or "—",
    )

    failed: list[str] = []
    for name, script in STEPS:
        if not run_step(name, script):
            # Крок, що впав, НЕ зупиняє наступні (докстрінг модуля):
            # збій LLM у синтезі не повинен забирати notify_forecasts.
            failed.append(name)

    if failed:
        logger.error("Провалені кроки: %s", ", ".join(failed))
        sys.exit(1)
    logger.info("Конвеєр пройдено повністю (%d кроків)", len(STEPS))


if __name__ == "__main__":
    main()
