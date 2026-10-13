#!/usr/bin/env python3
"""
Ручний запуск ОДНІЄЇ джоби негайно, без очікування розкладу — для
тестування orchestration/ і для дебагу конкретної джоби. Той самий
код/шлях, що й автоматичний запуск (runner.py) — тільки без
планувальника.

Використання:
    python orchestration/run_job.py check_releases
    python orchestration/run_job.py --list
"""

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jobs import JOBS  # noqa: E402
from runner import run_job  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    load_dotenv()

    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    if sys.argv[1] == "--list":
        for name in sorted(JOBS):
            print(name)
        sys.exit(0)

    job_name = sys.argv[1]
    if job_name not in JOBS:
        sys.exit(f"Невідома джоба: {job_name!r}. Доступні: {sorted(JOBS)}")

    ok = run_job(job_name)
    print(f"{'OK' if ok else 'ПРОВАЛ'}: {job_name} (лог — logs/{job_name}_*.log)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
