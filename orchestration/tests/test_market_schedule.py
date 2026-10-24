"""
Календар ринків у розкладі (2026-10-24): заплановані джоби на закритих
ринках не запускаються, а ручний запуск — запускається завжди.
"""

import runner
from schedule import SCHEDULE

import config

GATE_KEYS = ("markets", "session", "after_session")
KNOWN_CLASSES = {"stock", "index", "fx", "commodity", "crypto"}


def test_gate_keys_reference_known_classes_and_sessions():
    for name, cfg in SCHEDULE.items():
        for asset_class in cfg.get("markets", ()):
            assert asset_class in KNOWN_CLASSES, name
        if "session" in cfg:
            assert cfg["session"] in config.SESSION_EXCHANGES, name
        if "after_session" in cfg:
            assert cfg["after_session"] in KNOWN_CLASSES, name


def test_session_briefs_are_gated_by_their_exchange():
    for session in ("asia", "europe", "us"):
        assert SCHEDULE[f"market_synthesis_{session}"]["session"] == session


def test_crypto_jobs_are_never_gated():
    for name in ("crypto_prices", "crypto_screening_daily", "notify_crypto_screening", "notify_crypto_long"):
        assert not any(key in SCHEDULE[name] for key in GATE_KEYS), name


def test_weekend_wrap_runs_on_sunday_before_its_notifier():
    wrap, notify = SCHEDULE["weekend_wrap"]["trigger"], SCHEDULE["notify_weekend_wrap"]["trigger"]
    assert wrap["day_of_week"] == notify["day_of_week"] == "sun"
    assert (wrap["hour"], wrap["minute"]) < (notify["hour"], notify["minute"])


def test_scheduled_run_skips_closed_market_and_logs_it(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "LOG_DIR", tmp_path)
    monkeypatch.setattr(runner, "run_job", lambda name: (_ for _ in ()).throw(AssertionError("не мав запускатись")))
    import common.market_hours as market_hours
    monkeypatch.setattr(market_hours, "schedule_gate", lambda cfg, at=None: "ринки закриті (stock)")

    assert runner.run_scheduled("notify_screening", {"after_session": "stock"}) is True
    log = next(tmp_path.glob("notify_screening_*.log")).read_text()
    assert "ПРОПУЩЕНО: ринки закриті (stock)" in log


def test_scheduled_run_without_gate_keys_runs_job(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_job", lambda name: calls.append(name) or True)
    assert runner.run_scheduled("crypto_prices", {"trigger": {"minute": 0}}) is True
    assert calls == ["crypto_prices"]
