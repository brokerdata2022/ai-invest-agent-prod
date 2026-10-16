"""
Тести send_telegram_message() — parse_mode опціональний (2026-10-03):
за замовчуванням не передається в Telegram API взагалі (plain text,
той самий принцип, що й раніше — orchestration/alerts.py свідомо на
цьому лишається), явний parse_mode="HTML" додається в POST-дані.
"""

import telegram_client


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_send_telegram_message_without_parse_mode_omits_it(monkeypatch):
    # Емулюємо ПРОДАКШЕН: там AGENT_ENV не задано, тож транспорт шле.
    # Без цього тест залежав би від .env машини, на якій запущений
    # (у дев-контейнері AGENT_ENV=dev — і відправка свідомо блокується).
    monkeypatch.delenv("AGENT_ENV", raising=False)
    captured = {}

    def fake_post(url, data, timeout):
        captured.update(data=data, url=url, timeout=timeout)
        return _FakeResponse({"ok": True})

    monkeypatch.setattr(telegram_client.requests, "post", fake_post)

    telegram_client.send_telegram_message("TOKEN", "CHAT", "hello")

    assert "parse_mode" not in captured["data"]
    assert captured["data"] == {"chat_id": "CHAT", "text": "hello"}


def test_send_telegram_message_with_parse_mode_includes_it(monkeypatch):
    monkeypatch.delenv("AGENT_ENV", raising=False)
    captured = {}

    def fake_post(url, data, timeout):
        captured.update(data=data)
        return _FakeResponse({"ok": True})

    monkeypatch.setattr(telegram_client.requests, "post", fake_post)

    telegram_client.send_telegram_message("TOKEN", "CHAT", "<b>hi</b>", parse_mode="HTML")

    assert captured["data"]["parse_mode"] == "HTML"
    assert captured["data"]["text"] == "<b>hi</b>"


def test_get_updates_passes_offset_and_returns_result_list(monkeypatch):
    captured = {}

    def fake_get(url, params, timeout):
        captured.update(url=url, params=params, timeout=timeout)
        return _FakeResponse({"ok": True, "result": [{"update_id": 1}]})

    monkeypatch.setattr(telegram_client.requests, "get", fake_get)

    updates = telegram_client.get_updates("TOKEN", offset=42, timeout=0)

    assert updates == [{"update_id": 1}]
    assert captured["params"] == {"timeout": 0, "offset": 42}
    assert "getUpdates" in captured["url"]


def test_get_updates_omits_offset_when_not_given(monkeypatch):
    captured = {}

    def fake_get(url, params, timeout):
        captured.update(params=params)
        return _FakeResponse({"ok": True, "result": []})

    monkeypatch.setattr(telegram_client.requests, "get", fake_get)

    telegram_client.get_updates("TOKEN")

    assert "offset" not in captured["params"]


def test_get_updates_defaults_to_empty_list_when_result_missing(monkeypatch):
    monkeypatch.setattr(
        telegram_client.requests, "get",
        lambda url, params, timeout: _FakeResponse({"ok": True}),
    )

    assert telegram_client.get_updates("TOKEN") == []


def test_set_my_commands_posts_commands_payload(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured.update(url=url, json=json)
        return _FakeResponse({"ok": True})

    monkeypatch.setattr(telegram_client.requests, "post", fake_post)

    commands = [{"command": "notify_screening", "description": "Надіслати скринінг S&P 500"}]
    telegram_client.set_my_commands("TOKEN", commands)

    assert captured["json"] == {"commands": commands}
    assert "setMyCommands" in captured["url"]


# ============================================================
# Запобіжник середовища (2026-10-16)
# ============================================================
#
# Живий випадок: дев і продакшен мають ОДИН токен бота, і ручний
# прогін у деві надіслав користувачу відповідь, зібрану з дев-бази —
# знімка триденної давності, зовні невідрізнимого від справжнього
# звіту агента.

import pytest

from telegram_client import NON_PRODUCTION_ENVS, is_sending_blocked, send_telegram_message


@pytest.mark.parametrize("env", sorted(NON_PRODUCTION_ENVS))
def test_non_production_env_blocks_sending(monkeypatch, env):
    monkeypatch.setenv("AGENT_ENV", env)

    assert is_sending_blocked() == env


@pytest.mark.parametrize("env", ["", "production", "prod", "PRODUCTION", "  "])
def test_production_and_unset_allow_sending(monkeypatch, env):
    """Асиметрія навмисна: якби дозволялось лише явно позначене
    'production', відсутність змінної в продакшен-.env ТИХО вимкнула б
    усі сповіщення агента."""
    monkeypatch.setenv("AGENT_ENV", env)

    assert is_sending_blocked() is None


def test_unset_variable_allows_sending(monkeypatch):
    monkeypatch.delenv("AGENT_ENV", raising=False)

    assert is_sending_blocked() is None


def test_case_and_spaces_do_not_bypass_the_guard(monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "  DeV  ")

    assert is_sending_blocked() == "dev"


def test_blocked_send_makes_no_http_call(monkeypatch):
    """Головне: у заблокованому середовищі мережевого виклику НЕМА
    взагалі — не «надіслали й проігнорували відповідь»."""
    called = []
    monkeypatch.setenv("AGENT_ENV", "dev")
    monkeypatch.setattr(
        "telegram_client.requests.post",
        lambda *args, **kwargs: called.append(args) or (_ for _ in ()).throw(AssertionError("не має викликатись")),
    )
    result = send_telegram_message("token:123", "42", "текст")

    assert result == {"ok": False, "skipped_env": "dev"}
    assert called == []
