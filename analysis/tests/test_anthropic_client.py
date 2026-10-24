"""
Тести Anthropic-клієнта на фейковому client-об'єкті — без мережі й без
встановленого SDK (той самий принцип, що адаптери в data-ingestion:
подаємо фейкову відповідь API).

Стережуть саме ті три речі, де легко скопіювати DeepSeek-клієнт і
зламати запит (докстрінг `anthropic_client.py`): `temperature`,
формат JSON і розмірковування/`max_tokens`.
"""

import pytest

from anthropic_client import AnthropicError, call_anthropic, call_anthropic_batch


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


# ============================================================
# Розмірковування як налаштування (2026-10-24)
# ============================================================


def test_thinking_not_sent_by_default():
    """Дефолт `thinking=None` — поле не передається, модель думає за
    своїм дефолтом, як і до появи налаштування (поведінка не змінилась)."""
    client = _FakeClient(_Response([_Block("{}")]))
    call_anthropic("prompt", api_key="k", effort="medium", client=client)
    assert "thinking" not in client.messages.last_request


@pytest.mark.parametrize("mode", ["adaptive", "disabled"])
def test_thinking_mode_is_passed_explicitly(mode):
    client = _FakeClient(_Response([_Block("{}")]))
    call_anthropic("prompt", api_key="k", thinking=mode, client=client)
    assert client.messages.last_request["thinking"] == {"type": mode}


# ============================================================
# Message Batches API (filing_insight, 2026-10-24)
# ============================================================


class _Batch:
    def __init__(self, status):
        self.id = "msgbatch_1"
        self.processing_status = status


class _Result:
    def __init__(self, type, message=None):
        self.type = type
        self.message = message


class _Entry:
    def __init__(self, custom_id, result):
        self.custom_id = custom_id
        self.result = result


class _FakeBatches:
    def __init__(self, statuses, entries):
        self._statuses = list(statuses)
        self._entries = entries
        self.created = None
        self.cancelled = False

    def create(self, requests):
        self.created = requests
        return _Batch(self._statuses.pop(0))

    def retrieve(self, batch_id):
        return _Batch(self._statuses.pop(0) if self._statuses else "ended")

    def results(self, batch_id):
        return iter(self._entries)

    def cancel(self, batch_id):
        self.cancelled = True


class _BatchClient:
    def __init__(self, batches):
        self.messages = type("M", (), {"batches": batches})()


def test_batch_maps_results_back_to_caller_keys():
    """custom_id в API — порядкові номери (тикер із крапкою чи двокрапкою
    був би недопустимим), а викликач отримує свої ключі назад."""
    batches = _FakeBatches(
        ["in_progress", "ended"],
        [
            _Entry("r1", _Result("succeeded", _Response([_Block('{"b": 2}')]))),
            _Entry("r0", _Result("succeeded", _Response([_Block('{"a": 1}')]))),
        ],
    )
    out = call_anthropic_batch(
        [("AAPL:0001", "p1"), ("BRK.B:0002", "p2")], api_key="k",
        effort="medium", thinking="disabled", client=_BatchClient(batches), sleep=lambda s: None,
    )
    assert out == {"AAPL:0001": '{"a": 1}', "BRK.B:0002": '{"b": 2}'}
    assert [r["custom_id"] for r in batches.created] == ["r0", "r1"]
    # Налаштування глибини йдуть у кожен запит пакета так само, як у звичайний виклик.
    params = batches.created[0]["params"]
    assert params["output_config"] == {"effort": "medium"}
    assert params["thinking"] == {"type": "disabled"}


def test_batch_error_of_one_request_does_not_fail_others():
    batches = _FakeBatches(
        ["ended"],
        [
            _Entry("r0", _Result("succeeded", _Response([_Block("ok")]))),
            _Entry("r1", _Result("errored")),
        ],
    )
    out = call_anthropic_batch(
        [("a", "p1"), ("b", "p2"), ("c", "p3")], api_key="k",
        client=_BatchClient(batches), sleep=lambda s: None,
    )
    assert out["a"] == "ok"
    assert isinstance(out["b"], AnthropicError)
    # Запит без результату в пакеті — теж помилка, не тиша.
    assert isinstance(out["c"], AnthropicError)


def test_batch_timeout_cancels_and_raises():
    batches = _FakeBatches(["in_progress"] * 10, [])
    with pytest.raises(AnthropicError, match="не завершився"):
        call_anthropic_batch(
            [("a", "p")], api_key="k", poll_seconds=30, max_wait_seconds=60,
            client=_BatchClient(batches), sleep=lambda s: None,
        )
    assert batches.cancelled
