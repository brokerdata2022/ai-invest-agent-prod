#!/usr/bin/env python3
"""
ЗДОРОВ'Я ВИВОДІВ: чи виводи агента взагалі щось РІЗНЯТЬ.

**Постановка (рішення користувача 2026-10-15).** Домінантний клас
дефектів цього проєкту — не падіння, а виродження: джоба відпрацювала,
записала рядки, і вони нічого не значать. Шість таких випадків 13.10 і
два 15.10, і **жоден не впав сам** — усі знайдені виміром руками в
сесії. Два наявні контролери відповідають на інші питання:
`common/freshness.py` — «чи дані свіжі», `common/quality.py` — «чи їх
не переписали». Питання «чи вивід узагалі різнить активи» не ставив
ніхто.

**Три живі приклади, які ця джоба ловить:**
- `market_synthesis.cluster_count` = 8 у ВСІХ рядках (пошук дублікатів
  не знаходив їх узагалі) → перевірка `min_distinct`;
- `catalyst_score` із двох значень на добу на 29 активів → `min_distinct`;
- `news_consolidated.source_count` = 1 у всіх рядках → `min_distinct`.

**Чому алерт лише при ЗМІНІ стану.** Перевірка, що щодня звітує «усе
гаразд», стає шумом і її перестають читати — це вже траплялось двічі
(прапорець нестабільних ревізій висів на 8 з 11 активів; звірка
FRED↔BLS шле лише розбіжності). Тому історія пишеться завжди, а
Telegram отримує лише переходи ok→fail і fail→ok.

**Що НЕ є дефектом.** Порожня подієва таблиця (`min_rows: 0`) —
нормальний стан: прогнози пишуться після релізів, кандидати бувають
відсутні. Такі перевірки дають `skipped`, не `fail`.

Використання:
    python output_health.py
    python output_health.py --notify      # + Telegram при ЗМІНІ стану
    python output_health.py --dry-run     # не писати історію
"""

import argparse
import json
import logging
import os
import re
import sys

from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_REPO_ROOT, "data-ingestion"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "reporting"))

from common.db import get_connection  # noqa: E402

import config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Перевіряє, чи виводи агента не виродились (кількість рядків, "
    "різноманіття значень, частка NULL) і шле Telegram лише при зміні стану."
)

OK = "ok"
FAIL = "fail"
SKIPPED = "skipped"

# Імена таблиць і колонок ідуть у SQL підстановкою, бо параметризувати
# ІДЕНТИФІКАТОРИ не можна. Тому вони проходять білий список: конфіг
# редагує людина, і друкарська помилка не має ставати чимось гіршим за
# помилку.
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _safe_ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"недопустиме імʼя в OUTPUT_HEALTH_CHECKS: {name!r}")
    return name


def _window_clause(check: dict) -> str:
    """Подієві таблиці (`window_hours: None`) міряються цілком — вік
    рядків у них нічого не означає."""
    if check["window_hours"] is None:
        return ""
    column = _safe_ident(check["time_column"])
    return f"WHERE {column} >= now() - interval '{int(check['window_hours'])} hours'"


def run_check(conn, check: dict) -> dict:
    """Один вимір. Повертає {status, measured, detail} — рішення про
    алерт приймається окремо (історія в БД)."""
    table = _safe_ident(check["table"])
    where = _window_clause(check)

    selects = ["count(*) AS rows"]
    for field in check["vary"]:
        selects.append(f"count(DISTINCT {_safe_ident(field)}) AS distinct_{_safe_ident(field)}")
    for field in check["required"]:
        selects.append(
            f"count(*) FILTER (WHERE {_safe_ident(field)} IS NULL) AS nulls_{_safe_ident(field)}"
        )

    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(selects)} FROM {table} {where}")  # noqa: S608
        columns = [d[0] for d in cur.description]
        measured = dict(zip(columns, cur.fetchone()))

    rows = measured["rows"]
    problems: list[str] = []

    if rows == 0:
        if check["min_rows"] == 0:
            return {
                "status": SKIPPED,
                "measured": measured,
                "detail": "рядків немає — для подієвої таблиці це норма",
            }
        # Таблиця порожня ЦІЛКОМ (не лише у вікні) — механізм ще не
        # починав працювати: нова функція, свіже розгортання. Це не
        # виродження виводу, і кричати про це означало б вчити
        # користувача ігнорувати алерти.
        with conn.cursor() as cur:
            cur.execute(f"SELECT EXISTS (SELECT 1 FROM {table})")  # noqa: S608
            has_any = cur.fetchone()[0]
        if not has_any:
            return {
                "status": SKIPPED,
                "measured": measured,
                "detail": "таблиця ще порожня — механізм не починав працювати",
            }
        return {
            "status": FAIL,
            "measured": measured,
            "detail": (
                f"жодного рядка за вікно (очікувалось ≥{check['min_rows']}), "
                "хоча раніше рядки були — джоба перестала писати"
            ),
        }

    if rows < check["min_rows"]:
        problems.append(f"рядків {rows} < {check['min_rows']}")

    # Поріг різноманітності — не більший за кількість рядків: 2 рядки
    # не можуть мати 3 різні значення, і без цього короткий (законно)
    # список торгів читався б як вироджений (2026-10-22).
    min_distinct = min(check["min_distinct"], rows)
    for field in check["vary"]:
        distinct = measured[f"distinct_{field}"]
        if distinct < min_distinct:
            # Саме це й означає "вивід вироджений": рядки є, а значення
            # в них однакові.
            problems.append(
                f"{field}: лише {distinct} різних значень на {rows} рядків "
                f"(очікувалось ≥{min_distinct})"
            )

    for field in check["required"]:
        share = measured[f"nulls_{field}"] / rows
        if share > check["max_null_share"]:
            problems.append(f"{field}: NULL у {share:.0%} рядків (межа {check['max_null_share']:.0%})")

    if problems:
        return {"status": FAIL, "measured": measured, "detail": "; ".join(problems)}

    varied = ", ".join(f"{f}={measured[f'distinct_{f}']}" for f in check["vary"])
    return {
        "status": OK,
        "measured": measured,
        "detail": f"рядків {rows}" + (f", різних значень: {varied}" if varied else ""),
    }


def previous_status(conn, check_name: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status FROM output_health_checks
            WHERE check_name = %s ORDER BY checked_at DESC LIMIT 1
            """,
            (check_name,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def save_result(conn, check: dict, result: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO output_health_checks (check_name, target_table, status, measured, detail)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (check["name"], check["table"], result["status"],
             json.dumps(result["measured"], default=str), result["detail"]),
        )
    conn.commit()


def format_alert(changes: list[dict]) -> str:
    broke = [c for c in changes if c["result"]["status"] == FAIL]
    fixed = [c for c in changes if c["result"]["status"] == OK]
    lines: list[str] = []

    if broke:
        lines.append("⚠️ <b>Вивід виродився</b>")
        lines.append("")
        for item in broke:
            lines.append(f"• <b>{item['check']['table']}</b> ({item['check']['name']})")
            lines.append(f"  {item['result']['detail']}")
        lines.append("")
        lines.append(
            "Рядки є, але вони нічого не різнять — цей клас дефектів не "
            "падає сам і не видно в тестах. Дивитись джобу, що пише цю "
            "таблицю."
        )
    if fixed:
        if broke:
            lines.append("")
        lines.append("✅ <b>Відновилось</b>")
        for item in fixed:
            lines.append(f"• {item['check']['table']}: {item['result']['detail']}")

    return "\n".join(lines)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--notify", action="store_true", help="Telegram при ЗМІНІ стану перевірки")
    parser.add_argument("--dry-run", action="store_true", help="не писати історію перевірок")
    args = parser.parse_args()

    conn = get_connection()
    changes: list[dict] = []
    try:
        for check in config.OUTPUT_HEALTH_CHECKS:
            try:
                result = run_check(conn, check)
            except Exception:
                # Збій однієї перевірки не має забирати решту: таблиці
                # з'являються й міняються, а моніторинг мусить вижити.
                logger.error("%s: перевірка впала", check["name"], exc_info=True)
                conn.rollback()
                continue

            before = previous_status(conn, check["name"])
            logger.info(
                "%s [%s] %s — %s", check["name"], check["table"],
                result["status"].upper(), result["detail"],
            )
            if not args.dry_run:
                save_result(conn, check, result)
            # Базова лінія для порівняння — попередній стан, а за його
            # відсутності OK. Інакше перевірка, що впала ПЕРШОГО ж разу і
            # лишається FAIL, не сповістила б ніколи: порівнювати було б
            # ні з чим, а далі стан не змінюється.
            baseline = before if before is not None else OK
            if result["status"] != SKIPPED and baseline != result["status"]:
                changes.append({"check": check, "result": result, "before": baseline})

        failing = [c["check"]["name"] for c in changes if c["result"]["status"] == FAIL]
        if failing:
            logger.warning("Змінили стан на FAIL: %s", ", ".join(failing))

        if args.notify and changes:
            from _common import send_report  # noqa: E402

            send_report(format_alert(changes))
            logger.info("Надіслано алерт про %d змін стану", len(changes))
        elif args.notify:
            logger.info(
                "Стан перевірок не змінився — сповіщення НЕ надсилається "
                "(щоденне 'усе гаразд' стає шумом)"
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
