"""
Тести determine_scope() (чиста логіка, без БД) і run_outlook() з
підміненими БД/LLM-функціями — той самий підхід, що
test_update_forecasts.py (monkeypatch, без реальної БД/мережі).
"""

from datetime import date

from calendar_outlook.run_outlook import NO_EVENTS_SUMMARY, determine_scope, run_outlook
from llm_common import LlmResponse, SynthesisResult

# Фейкова відповідь LLM — провайдер/модель тут не важливі, важливо що
# це LlmResponse, як віддає справжній call_llm (2026-10-05).
_LLM = LlmResponse(text="RAW", provider="deepseek", model="deepseek-chat")

MONDAY = date(2026, 10, 5)
TUESDAY = date(2026, 10, 6)
FRIDAY = date(2026, 10, 9)
SATURDAY = date(2026, 10, 10)
SUNDAY = date(2026, 10, 11)

ENTRY = {"id": 7, "source": "fred", "metric_id": "cpi", "scheduled_at": "2026-10-06 12:30", "impact_level": "high", "expected_value": "0.4%"}


def test_determine_scope_monday_returns_both_week_and_day():
    """2026-10-03, рішення користувача: в понеділок має бути ДВА огляди
    — на весь тиждень і ОКРЕМО на сьогодні, не тиждень замість дня."""
    scopes = determine_scope(MONDAY)
    assert scopes == [
        ("week", MONDAY, FRIDAY),
        ("day", MONDAY, MONDAY),
    ]


def test_determine_scope_midweek_is_day_only():
    assert determine_scope(TUESDAY) == [("day", TUESDAY, TUESDAY)]


def test_determine_scope_friday_is_day_only():
    assert determine_scope(FRIDAY) == [("day", FRIDAY, FRIDAY)]


def test_determine_scope_weekend_is_empty():
    assert determine_scope(SATURDAY) == []
    assert determine_scope(SUNDAY) == []


def test_run_outlook_skips_weekend_without_touching_db(monkeypatch):
    calls = []
    monkeypatch.setattr("calendar_outlook.run_outlook.has_outlook", lambda *a, **k: calls.append("has_outlook"))

    result = run_outlook(conn=None, today=SATURDAY)

    assert result == []
    assert calls == []


def test_run_outlook_skips_when_already_generated(monkeypatch):
    monkeypatch.setattr("calendar_outlook.run_outlook.has_outlook", lambda conn, outlook_date, scope: True)
    monkeypatch.setattr(
        "calendar_outlook.run_outlook.fetch_upcoming",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("не мало викликатись")),
    )

    result = run_outlook(conn=None, today=TUESDAY)

    assert result == []


def test_run_outlook_no_events_skips_llm_but_still_saves(monkeypatch):
    saved = {}
    monkeypatch.setattr("calendar_outlook.run_outlook.has_outlook", lambda *a, **k: False)
    monkeypatch.setattr("calendar_outlook.run_outlook.fetch_upcoming", lambda *a, **k: [])
    monkeypatch.setattr(
        "calendar_outlook.run_outlook.synthesize_outlook",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM не мав викликатись без подій")),
    )
    monkeypatch.setattr(
        "calendar_outlook.run_outlook.save_outlook",
        lambda conn, outlook_date, scope, release_log_ids, result, llm_call_id: saved.update(
            outlook_date=outlook_date, scope=scope, release_log_ids=release_log_ids,
            result=result, llm_call_id=llm_call_id,
        ) or 1,
    )

    outlook_ids = run_outlook(conn=None, today=TUESDAY)

    assert outlook_ids == [1]
    assert saved["release_log_ids"] == []
    assert saved["llm_call_id"] is None
    assert saved["result"].summary == NO_EVENTS_SUMMARY
    assert saved["result"].direction == "neutral"


def test_run_outlook_with_events_calls_llm_and_logs(monkeypatch):
    saved = {}
    logged = {}
    result = SynthesisResult(direction="up", confidence=0.7, summary="S", reasoning="R")

    monkeypatch.setattr("calendar_outlook.run_outlook.has_outlook", lambda *a, **k: False)
    monkeypatch.setattr("calendar_outlook.run_outlook.fetch_upcoming", lambda *a, **k: [ENTRY])
    monkeypatch.setattr(
        "calendar_outlook.run_outlook.synthesize_outlook",
        lambda entries, scope: (result, "PROMPT", _LLM),
    )
    monkeypatch.setattr(
        "calendar_outlook.run_outlook.log_llm_call",
        lambda conn, purpose, prompt, source_ref, llm: logged.update(
            purpose=purpose, prompt=prompt, response=llm.text,
        ) or 99,
    )
    monkeypatch.setattr(
        "calendar_outlook.run_outlook.save_outlook",
        lambda conn, outlook_date, scope, release_log_ids, result, llm_call_id: saved.update(
            release_log_ids=release_log_ids, result=result, llm_call_id=llm_call_id,
        ) or 2,
    )

    outlook_ids = run_outlook(conn=None, today=TUESDAY)

    assert outlook_ids == [2]
    assert saved["release_log_ids"] == [7]
    assert saved["llm_call_id"] == 99
    assert saved["result"] is result
    assert logged["purpose"] == "calendar_outlook"


def test_run_outlook_monday_generates_both_scopes_independently(monkeypatch):
    """Понеділок: 'week' уже існує (напр. ретрай після часткового
    провалу), 'day' — ще ні. Має згенерувати лише 'day', не пропустити
    обидва й не повторити LLM для вже збереженого 'week'."""
    existing = {"week"}
    fetched_scopes = []
    saved_scopes = []

    monkeypatch.setattr(
        "calendar_outlook.run_outlook.has_outlook",
        lambda conn, outlook_date, scope: scope in existing,
    )
    monkeypatch.setattr(
        "calendar_outlook.run_outlook.fetch_upcoming",
        lambda conn, start, end: fetched_scopes.append((start, end)) or [],
    )
    monkeypatch.setattr(
        "calendar_outlook.run_outlook.save_outlook",
        lambda conn, outlook_date, scope, release_log_ids, result, llm_call_id: saved_scopes.append(scope) or 1,
    )

    outlook_ids = run_outlook(conn=None, today=MONDAY)

    assert outlook_ids == [1]
    assert saved_scopes == ["day"]
    assert fetched_scopes == [(MONDAY, MONDAY)]
