"""
КОЖЕН скрипт-джоба мусить імпортуватись — перевірка всіх, а не лише
тих, про які згадали.

Навіщо цей тест (живий збій 2026-10-05): консолідація порогів у єдиний
`config.py` у корені зламала продакшен. Корінь репозиторію я додав у
`sys.path` ВРУЧНУ в 7 скриптів і живо перевірив 4 модулі — а джоб, що
транзитивно тягнуть конфіг, значно більше. Уночі провалилось 7 джоб із
`ModuleNotFoundError: No module named 'config'` (synthesize_market,
consolidate × 3, merge_similar, watchlist_prices).

Урок: ручний перелік входів ненадійний за визначенням — покриває лише
те, про що згадали. Тест іде по `jobs.py:JOBS` і перевіряє ВСІ, тож
наступна зміна шляхів імпорту не доїде до продакшену тихо.

Перевірка — запуск скрипта з `--help`: argparse виходить до main(), але
ІМПОРТИ модуля вже виконані, тож будь-який ImportError проявиться.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from jobs import JOBS

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SUBPROCESS_JOBS = sorted(
    (name, cfg["subprocess"]) for name, cfg in JOBS.items() if "subprocess" in cfg
)
CALLABLE_JOBS = sorted(name for name, cfg in JOBS.items() if "callable" in cfg)

# Маркери збою ІМПОРТУ (не бізнес-помилки й не відсутніх креденшелів).
IMPORT_FAILURE_MARKERS = ("ModuleNotFoundError", "ImportError", "No module named")


@pytest.mark.parametrize("job_name,command", SUBPROCESS_JOBS, ids=[n for n, _ in SUBPROCESS_JOBS])
def test_job_script_imports_cleanly(job_name, command):
    """`--help` не чіпає БД/мережу, але виконує всі імпорти модуля."""
    script = command[1]
    assert Path(script).exists(), f"{job_name}: скрипта {script} не існує"

    result = subprocess.run(
        [sys.executable, script, "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = result.stdout + result.stderr

    for marker in IMPORT_FAILURE_MARKERS:
        assert marker not in combined, (
            f"{job_name} ({script}): збій імпорту\n{combined[-1500:]}"
        )


@pytest.mark.parametrize("job_name", CALLABLE_JOBS)
def test_callable_job_is_callable(job_name):
    """Callable-джоби (напр. `_watchlist_prices`) імпортують свої
    залежності ВСЕРЕДИНІ функції, тож ImportError проявиться лише при
    виконанні — тут перевіряємо принаймні, що обʼєкт викликається й
    зареєстрований коректно."""
    assert callable(JOBS[job_name]["callable"])


def test_every_job_has_exactly_one_kind():
    for name, cfg in JOBS.items():
        kinds = [k for k in ("subprocess", "callable") if k in cfg]
        assert len(kinds) == 1, f"{name}: має бути рівно один вид запуску, а є {kinds}"


def test_repo_root_importable_for_single_config():
    """Прямий корінь живого збою: `config.py` лежить у КОРЕНІ, тож
    корінь мусить бути в шляху імпорту. У продакшені це
    `ENV PYTHONPATH=/app` (Dockerfile), у тестах — `pytest.ini`."""
    import config

    assert hasattr(config, "ANOMALY_SIGMA_MULTIPLIER")
    assert hasattr(config, "ASSET_FACTORS")
