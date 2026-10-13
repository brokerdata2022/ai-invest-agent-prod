"""
Виконання ОДНІЄЇ джоби (subprocess-скрипт або Python-функція) з логом і
Telegram-алертом при провалі. Тільки виконання — жодної бізнес-логіки
(orchestration/CLAUDE.md).

Повторні спроби (2026-09-28, рішення користувача — docs/decisions.md
"критичні фікси звітності"): "джоби не мають провалюватися, бо ми не
отримуємо останні дані — це критично важливо". Транзієнтний мережевий
збій (таймаут до джерела, тимчасовий 429/5xx) — найчастіша причина
провалу джоб збору даних (docs/decisions.md, кілька живих прикладів), і
повторний прогін найчастіше просто спрацьовує — append-only дедуп
(rule 6 CLAUDE.md) робить повтор безпечним для БУДЬ-ЯКОЇ джоби проєкту,
тому ретрай тут увімкнено за замовчуванням для всіх, не per-job
white-list.
"""

import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alerts import notify_failure  # noqa: E402
from jobs import JOBS, REPO_ROOT  # noqa: E402

logger = logging.getLogger(__name__)

LOG_DIR = REPO_ROOT / "logs"
DEFAULT_TIMEOUT_SECONDS = 1800

# Скільки ДОДАТКОВИХ спроб робити після першого провалу, перш ніж
# здатись і сповістити Telegram (jobs.py:JOBS може перекрити на джобу
# через "retries" — 0 для важких годинних прогонів типу
# quotes_universe_refresh, де другий повний прогін одразу після
# провалу лише вдруге вперся б у те саме джерело без паузи).
# 1 = одна повторна спроба (2 виконання всього).
DEFAULT_RETRIES = 1
RETRY_BACKOFF_SECONDS = 60


def _log_path(job_name: str) -> Path:
    """Один файл на джобу за ДОБУ, не на виклик.

    Було: `{job}_{YYYYMMDDTHHMMSSZ}.log` — окремий файл на кожен
    виклик. Обґрунтування того варіанту ("~150 файлів на добу") стало
    хибним 2026-10-02, коли ланцюжок релізу
    (check_releases/update_forecasts/compare_expectations/
    synthesize_expectations/notify_expectations/notify_release_impact/
    notify_forecasts + telegram_commands) перевели на ЩОХВИЛИННИЙ
    тригер: 8 джоб × 1440 = 11 520 файлів на добу. Живий стан на
    2026-10-11: **96 364 файли / 384 МБ** у logs/, тобто ~12 тисяч на
    добу — у 80 разів більше за оцінку, під якою писався prune_logs.
    На VPS (project_vps_migration) це впирається в іноди раніше, ніж
    у диск.

    Добове групування нічого не втрачає — кожен прогін пише власний
    заголовок із UTC-часом (`_run_header`), тому "один прогін" і далі
    знаходиться grep'ом, — і дає 54 файли на добу замість 12 тисяч.
    `prune_logs` чистить за mtime, тож іменування йому однакове."""
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return LOG_DIR / f"{job_name}_{stamp}.log"


def _run_header() -> str:
    return datetime.now(timezone.utc).strftime("=== прогін %Y-%m-%dT%H:%M:%SZ ===")


def _decode(value) -> str:
    """subprocess.TimeoutExpired.stdout/.stderr лишаються BYTES навіть
    коли subprocess.run() викликано з text=True — text= декодує вивід
    лише на успішному шляху (CompletedProcess), не на винятку. Живий
    баг, знайдений 2026-09-27: quotes_universe_refresh дійсно провалився
    по timeout, але сам except-блок падав з `TypeError: can only
    concatenate str (not "bytes") to str` замість залогувати й надіслати
    Telegram-алерт (notify_failure) — провал ставав повністю невидимим."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _attempt_subprocess(spec: dict) -> tuple[bool, str, str]:
    """Одна спроба subprocess-джоби. Повертає (успіх, причина_провалу
    [порожньо при успіху], вивід)."""
    try:
        result = subprocess.run(
            spec["subprocess"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=spec.get("timeout", DEFAULT_TIMEOUT_SECONDS),
        )
    except subprocess.TimeoutExpired as exc:
        output = _decode(exc.stdout) + _decode(exc.stderr)
        return False, f"timeout ({exc.timeout}с)", output

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return False, f"exit code {result.returncode}", output
    return True, "", output


def _attempt_callable(spec: dict) -> tuple[bool, str, str]:
    """Одна спроба callable-джоби."""
    func = spec["callable"]
    try:
        func()
    except Exception as exc:  # noqa: BLE001 — навмисно широкий catch, той самий підхід, що collect_all.py
        return False, str(exc), f"{exc!r}\n"
    return True, "", "OK\n"


def run_job(job_name: str) -> bool:
    """Повертає True, якщо джоба виконалась успішно (subprocess exit 0
    або функція не кинула виняток) — за потреби з повторними спробами
    (див. докстрінг модуля). Лог — ОДИН файл на джобу за добу
    (`_log_path`), прогони дописуються з заголовком-таймстемпом, усі
    спроби одного прогону — під ним. Telegram-алерт (`notify_failure`) шле лише ОДИН раз,
    після того як вичерпано всі спроби — не спамити чат про кожну
    транзієнтну спробу, яку наступна ж могла виправити."""
    if job_name not in JOBS:
        raise KeyError(f"Невідома джоба: {job_name!r} (немає в orchestration/jobs.py:JOBS)")

    spec = JOBS[job_name]
    log_path = _log_path(job_name)
    total_attempts = spec.get("retries", DEFAULT_RETRIES) + 1

    # Накопичуємо текст спроб ЛИШЕ для Telegram-алерту при повному
    # провалі — у файл кожна спроба дописується одразу (_log_path:
    # один файл на джобу за добу), щоб лог існував навіть якщо
    # процес уб'ють посеред ретраїв.
    attempt_logs: list[str] = []
    success = False
    reason = ""
    output = ""

    for attempt in range(1, total_attempts + 1):
        logger.info("Старт джоби %s, спроба %d/%d (лог: %s)", job_name, attempt, total_attempts, log_path)

        if "subprocess" in spec:
            success, reason, output = _attempt_subprocess(spec)
        else:
            success, reason, output = _attempt_callable(spec)

        entry = f"--- спроба {attempt}/{total_attempts} ---\n{output}"
        attempt_logs.append(entry)
        with log_path.open("a", encoding="utf-8") as fh:
            if attempt == 1:
                fh.write(f"{_run_header()}\n")
            fh.write(f"{entry}\n")

        if success:
            logger.info("Джоба %s завершилась успішно (спроба %d/%d)", job_name, attempt, total_attempts)
            return True

        logger.error("Джоба %s провалилась (спроба %d/%d): %s", job_name, attempt, total_attempts, reason)
        if attempt < total_attempts:
            time.sleep(RETRY_BACKOFF_SECONDS)

    notify_failure(job_name, reason, "\n".join(attempt_logs))
    return False
