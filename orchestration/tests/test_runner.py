import subprocess

import runner


def _no_sleep(monkeypatch):
    """Ретрай-бекоф — реальний час у продакшені, у тестах завжди
    підмінюється на no-op (інакше тест повторної спроби реально чекав би
    RETRY_BACKOFF_SECONDS)."""
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)


def test_decode_handles_bytes_str_and_none():
    assert runner._decode(b"hello") == "hello"
    assert runner._decode("hello") == "hello"
    assert runner._decode(None) == ""


def test_timeout_expired_with_bytes_output_does_not_crash(tmp_path, monkeypatch):
    """Регресія 2026-09-27: subprocess.TimeoutExpired.stdout/.stderr лишаються
    bytes навіть при text=True (text= декодує лише успішний шлях), тому
    попередній `(exc.stdout or "") + (exc.stderr or "")` падав з TypeError —
    провал джоби (напр. quotes_universe_refresh по 30-хв timeout) ставав
    повністю невидимим: ані лог, ані Telegram-алерт. Дефолтний ретрай
    (DEFAULT_RETRIES=1) — тут обидві спроби так само падають по timeout,
    тест перевіряє, що алерт усе одно рівно один (не подвоюється)."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(runner, "LOG_DIR", tmp_path)
    monkeypatch.setattr(runner, "JOBS", {"slow_job": {"subprocess": ["sleep", "9999"]}})

    alerts: list[tuple[str, str, str]] = []
    monkeypatch.setattr(runner, "notify_failure", lambda name, reason, output: alerts.append((name, reason, output)))

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"], output=b"stdout bytes", stderr=b"stderr bytes")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    ok = runner.run_job("slow_job")

    assert ok is False
    assert len(alerts) == 1
    name, reason, output = alerts[0]
    assert name == "slow_job"
    assert "stdout bytes" in output
    assert "stderr bytes" in output

    log_files = list(tmp_path.glob("slow_job_*.log"))
    assert len(log_files) == 1
    assert "stdout bytes" in log_files[0].read_text()


def test_subprocess_job_retries_and_recovers_on_second_attempt(tmp_path, monkeypatch):
    """Критичний фікс 2026-09-28: транзієнтний збій (мережевий таймаут,
    тимчасовий 429/5xx джерела) не повинен рахуватись провалом джоби,
    якщо наступна спроба вдається — саме так найчастіше й трапляються
    провали джоб збору даних (docs/decisions.md)."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(runner, "LOG_DIR", tmp_path)
    monkeypatch.setattr(runner, "JOBS", {"flaky_job": {"subprocess": ["echo", "hi"]}})

    alerts = []
    monkeypatch.setattr(runner, "notify_failure", lambda *a: alerts.append(a))

    calls = {"n": 0}

    def fake_run(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return subprocess.CompletedProcess(args[0], returncode=1, stdout="fail once", stderr="")
        return subprocess.CompletedProcess(args[0], returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    ok = runner.run_job("flaky_job")

    assert ok is True
    assert calls["n"] == 2
    assert alerts == []  # успіх на ретраї — жодного алерту

    log_files = list(tmp_path.glob("flaky_job_*.log"))
    assert len(log_files) == 1  # один файл на ВИКЛИК джоби, не на спробу
    content = log_files[0].read_text()
    assert "спроба 1/2" in content
    assert "спроба 2/2" in content


def test_retries_zero_means_single_attempt_no_backoff(tmp_path, monkeypatch):
    """jobs.py може вимкнути ретрай для важких годинних джоб (напр.
    quotes_universe_refresh) через "retries": 0 — тоді провал одразу
    йде в Telegram, без другої спроби й без sleep."""
    monkeypatch.setattr(runner, "LOG_DIR", tmp_path)
    monkeypatch.setattr(runner, "JOBS", {"heavy_job": {"subprocess": ["false"], "retries": 0}})

    sleep_calls = []
    monkeypatch.setattr(runner.time, "sleep", lambda s: sleep_calls.append(s))

    alerts = []
    monkeypatch.setattr(runner, "notify_failure", lambda *a: alerts.append(a))

    calls = {"n": 0}

    def fake_run(*args, **kwargs):
        calls["n"] += 1
        return subprocess.CompletedProcess(args[0], returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    ok = runner.run_job("heavy_job")

    assert ok is False
    assert calls["n"] == 1
    assert len(alerts) == 1
    assert sleep_calls == []


def test_callable_job_retries_and_recovers(tmp_path, monkeypatch):
    """Той самий ретрай-контракт для callable-джоб (напр. crypto_prices),
    не тільки subprocess."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(runner, "LOG_DIR", tmp_path)

    calls = {"n": 0}

    def flaky_callable():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("тимчасовий мережевий збій")

    monkeypatch.setattr(runner, "JOBS", {"flaky_callable_job": {"callable": flaky_callable}})

    alerts = []
    monkeypatch.setattr(runner, "notify_failure", lambda *a: alerts.append(a))

    ok = runner.run_job("flaky_callable_job")

    assert ok is True
    assert calls["n"] == 2
    assert alerts == []


def test_callable_job_exhausts_retries_and_alerts_once(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(runner, "LOG_DIR", tmp_path)

    def always_fails():
        raise RuntimeError("постійний збій")

    monkeypatch.setattr(runner, "JOBS", {"broken_job": {"callable": always_fails}})

    alerts = []
    monkeypatch.setattr(runner, "notify_failure", lambda *a: alerts.append(a))

    ok = runner.run_job("broken_job")

    assert ok is False
    assert len(alerts) == 1
    assert "постійний збій" in alerts[0][1]
