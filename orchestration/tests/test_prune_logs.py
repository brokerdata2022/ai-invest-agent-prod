"""
Тести jobs.py:_prune_logs() — на тимчасовій теці, без реальних logs/
проєкту (та сама ізоляція, що в решті тестів: жодного справжнього
запису у робочий каталог).
"""

import os
import time
from datetime import datetime, timedelta, timezone

import jobs
from jobs import LOG_RETENTION_DAYS, _prune_logs


def _make_log(directory, name: str, age_days: float) -> None:
    path = directory / name
    path.write_text("вміст логу\n")
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))


def test_prunes_only_files_older_than_retention(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _make_log(log_dir, "check_releases_old.log", LOG_RETENTION_DAYS + 1)
    _make_log(log_dir, "check_releases_fresh.log", 1)
    _make_log(log_dir, "check_releases_edge.log", LOG_RETENTION_DAYS - 0.5)

    monkeypatch.setattr(jobs, "REPO_ROOT", tmp_path)
    _prune_logs()

    remaining = {p.name for p in log_dir.glob("*.log")}
    assert remaining == {"check_releases_fresh.log", "check_releases_edge.log"}


def test_leaves_non_log_files_alone(tmp_path, monkeypatch):
    """У logs/ може лежати й не-.log (напр. bootstrap_run.log користувача
    чи випадковий файл) — прибираємо лише за маскою *.log, нічого
    більше."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _make_log(log_dir, "old.log", LOG_RETENTION_DAYS + 5)
    other = log_dir / "notes.txt"
    other.write_text("не лог")
    os.utime(other, (time.time() - 90 * 86400,) * 2)

    monkeypatch.setattr(jobs, "REPO_ROOT", tmp_path)
    _prune_logs()

    assert not (log_dir / "old.log").exists()
    assert other.exists()


def test_missing_logs_dir_is_not_an_error(tmp_path, monkeypatch):
    """Свіже розгортання: жодна джоба ще не виконувалась, logs/ нема —
    джоба має тихо завершитись успішно, не впасти (інакше runner.py
    надіслав би Telegram-алерт на порожньому місці)."""
    monkeypatch.setattr(jobs, "REPO_ROOT", tmp_path)
    _prune_logs()  # не повинно кинути


def test_retention_window_is_the_documented_one():
    # Значення живе в одному місці (jobs.py) і згадане в schedule.py:why
    # та docs/ — тест ловить розсинхрон, якщо його зміняли "на місці".
    assert LOG_RETENTION_DAYS == 14
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOG_RETENTION_DAYS)
    assert cutoff < datetime.now(timezone.utc)
