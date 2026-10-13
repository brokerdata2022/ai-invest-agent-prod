"""
Тести call_deepseek() з підміненою requests.Session (fake session,
той самий підхід ін'єкції session, що й у data-ingestion-адаптерах)
— жодних реальних запитів до DeepSeek.
"""

import pytest

from news_analysis.deepseek_client import DeepSeekError, call_deepseek


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload
        self.last_call = None

    def post(self, url, headers=None, json=None, timeout=None):
        self.last_call = {"url": url, "headers": headers, "json": json, "timeout": timeout}
        return _FakeResponse(self._payload)


def test_call_deepseek_returns_message_content():
    session = _FakeSession(
        {"choices": [{"message": {"content": '{"ok": true}'}}]}
    )
    result = call_deepseek("prompt text", api_key="fake-key", session=session)
    assert result == '{"ok": true}'


def test_call_deepseek_sends_auth_header_and_system_prompt():
    session = _FakeSession({"choices": [{"message": {"content": "{}"}}]})
    call_deepseek("prompt", api_key="fake-key", system_prompt="be terse", session=session)

    assert session.last_call["headers"]["Authorization"] == "Bearer fake-key"
    messages = session.last_call["json"]["messages"]
    assert messages[0] == {"role": "system", "content": "be terse"}
    assert messages[1] == {"role": "user", "content": "prompt"}


def test_call_deepseek_raises_on_unexpected_payload():
    session = _FakeSession({"unexpected": "shape"})
    with pytest.raises(DeepSeekError):
        call_deepseek("prompt", api_key="fake-key", session=session)
