"""
Тести Anthropic-клієнта на фейковому client-об'єкті — без мережі й без
встановленого SDK (той самий принцип, що адаптери в data-ingestion:
подаємо фейкову відповідь API).

Стережуть саме ті три речі, де легко скопіювати DeepSeek-клієнт і
зламати запит (докстрінг `anthropic_client.py`): `temperature`,
формат JSON і розмірковування/`max_tokens`.
"""

import pytest

from anthropic_client import AnthropicError, call_anthropic


class _Block:
    def __init__(self, text, type="text"):
        self.text = text
        self.type = type


class _Usage:
    input_tokens = 100
    output_tokens = 200


class _Response:
    def __init__(self, blocks, stop_reason="end_turn", stop_details=None):
        self.content = blocks
        self.stop_reason = stop_reason
        self.stop_details = stop_details
        self.usage = _Usage()


class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.last_request = None

    def create(self, **kwargs):
        self.last_request = kwargs
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


def test_returns_text_of_all_text_blocks():
    client = _FakeClient(_Response([_Block('{"a": 1}'), _Block(" продовження")]))
    assert call_anthropic("prompt", api_key="k", client=client) == '{"a": 1}\n продовження'


def test_never_sends_temperature():
    """На Opus 5 / Sonnet 5 параметри семплінгу прибрані й повертають
    400 — скопійований із DeepSeek `temperature=0.0` зламав би КОЖЕН
    виклик."""
    client = _FakeClient(_Response([_Block("{}")]))
    call_anthropic("prompt", api_key="k", client=client)

    request = client.messages.last_request
    for forbidden in ("temperature", "top_p", "top_k", "response_format"):
        assert forbidden not in request


def test_passes_model_effort_and_max_tokens():
    client = _FakeClient(_Response([_Block("{}")]))
    call_anthropic(
        "prompt", api_key="k", system_prompt="sys",
        model="claude-opus-5", effort="high", max_tokens=8192, client=client,
    )

    request = client.messages.last_request
    assert request["model"] == "claude-opus-5"
    # effort — усередині output_config, не на верхньому рівні.
    assert request["output_config"] == {"effort": "high"}
    assert request["max_tokens"] == 8192
    assert request["system"] == "sys"
    assert request["messages"] == [{"role": "user", "content": "prompt"}]


def test_omits_output_config_without_effort():
    client = _FakeClient(_Response([_Block("{}")]))
    call_anthropic("prompt", api_key="k", effort=None, client=client)
    assert "output_config" not in client.messages.last_request


def test_refusal_raises_instead_of_empty_content():
    """Відмова класифікатора приходить як HTTP 200 — без перевірки код
    пішов би читати порожній content і впав незрозуміло."""
    class _Details:
        category = "cyber"

    client = _FakeClient(_Response([], stop_reason="refusal", stop_details=_Details()))
    with pytest.raises(AnthropicError) as exc:
        call_anthropic("prompt", api_key="k", client=client)
    assert "refusal" in str(exc.value)


def test_no_text_blocks_raises_with_hint():
    """Типова причина — max_tokens вичерпано розмірковуванням; у тексті
    помилки це має бути сказано, бо інакше діагностика займає годину."""
    client = _FakeClient(_Response([_Block("", type="thinking")], stop_reason="max_tokens"))
    with pytest.raises(AnthropicError) as exc:
        call_anthropic("prompt", api_key="k", client=client)
    assert "max_tokens" in str(exc.value)


def test_thinking_blocks_are_not_mistaken_for_answer():
    client = _FakeClient(_Response([_Block("роздуми", type="thinking"), _Block('{"ok": true}')]))
    assert call_anthropic("prompt", api_key="k", client=client) == '{"ok": true}'
