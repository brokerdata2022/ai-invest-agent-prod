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
    captured = {}

    def fake_post(url, data, timeout):
        captured.update(data=data, url=url, timeout=timeout)
        return _FakeResponse({"ok": True})

    monkeypatch.setattr(telegram_client.requests, "post", fake_post)

    telegram_client.send_telegram_message("TOKEN", "CHAT", "hello")

    assert "parse_mode" not in captured["data"]
    assert captured["data"] == {"chat_id": "CHAT", "text": "hello"}


def test_send_telegram_message_with_parse_mode_includes_it(monkeypatch):
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
