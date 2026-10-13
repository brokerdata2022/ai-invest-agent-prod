"""
Тести спільної LLM-проводки (llm_common.py) — вибір провайдера, розбір
структурованої відповіді, валідація confidence. Чисті функції, без
мережі й без БД.

Раніше ці самі перевірки були продубльовані в test_synthesize.py,
test_synthesize_market.py і test_synthesize_expectations.py (усі троє
тестували байт-у-байт однаковий parse_response/call_llm). Тепер
контракт перевіряється тут один раз, а per-скриптові тести лишають
собі лише те, що справді своє: build_prompt() і synthesize_*().
"""

import json

import pytest

import requests

import config
import llm_common
from llm_common import (
    DIRECTIONS,
    LlmError,
    SynthesisResponseError,
    call_llm,
    parse_confidence,
    parse_json_object,
    parse_synthesis_response,
    require_api_key,
    resolve_provider,
    resolve_route,
)

VALID_RESPONSE = {
    "direction": "up",
    "confidence": 0.75,
    "summary": "Факт вийшов вище очікувань.",
    "reasoning": "Сюрприз позитивний, 3 незалежні джерела.",
}


def test_directions_cover_documented_contract():
    # analysis/CLAUDE.md "Формат виходу LLM-аналізу"
    assert DIRECTIONS == {"up", "down", "neutral", "unclear"}


def test_parse_synthesis_response_valid():
    result = parse_synthesis_response(json.dumps(VALID_RESPONSE))
    assert result.direction == "up"
    assert result.confidence == 0.75
    assert result.summary == VALID_RESPONSE["summary"]
    assert result.reasoning == VALID_RESPONSE["reasoning"]


def test_parse_synthesis_response_rejects_invalid_json():
    with pytest.raises(SynthesisResponseError):
        parse_synthesis_response("not json")


def test_parse_synthesis_response_rejects_non_object_json():
    with pytest.raises(SynthesisResponseError):
        parse_synthesis_response("[1, 2, 3]")


@pytest.mark.parametrize("field", ["direction", "confidence", "summary", "reasoning"])
def test_parse_synthesis_response_rejects_missing_field(field):
    data = dict(VALID_RESPONSE)
    del data[field]
    with pytest.raises(SynthesisResponseError):
        parse_synthesis_response(json.dumps(data))


def test_parse_synthesis_response_rejects_unknown_direction():
    data = dict(VALID_RESPONSE, direction="sideways")
    with pytest.raises(SynthesisResponseError):
        parse_synthesis_response(json.dumps(data))


@pytest.mark.parametrize("bad", [1.5, -0.1, "дуже впевнений", None])
def test_parse_synthesis_response_rejects_bad_confidence(bad):
    data = dict(VALID_RESPONSE, confidence=bad)
    with pytest.raises(SynthesisResponseError):
        parse_synthesis_response(json.dumps(data))


def test_parse_confidence_accepts_bounds():
    assert parse_confidence(0) == 0.0
    assert parse_confidence(1) == 1.0


def test_parse_json_object_uses_given_error_class():
    class MyError(ValueError):
        pass

    with pytest.raises(MyError):
        parse_json_object("not json", MyError)


def test_resolve_provider_defaults_to_deepseek(monkeypatch):
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    assert resolve_provider() == "deepseek"


def test_resolve_provider_reads_env_at_call_time(monkeypatch):
    """Не константа рівня модуля — інакше значення "замерзало" на стані
    env під час імпорту (причина, чому раніше кожен скрипт мусив
    monkeypatch-ити власну LLM_PROVIDER)."""
    monkeypatch.setenv("SYNTHESIS_LLM_PROVIDER", "anthropic")
    assert resolve_provider() == "anthropic"


def test_resolve_provider_treats_empty_env_as_default(monkeypatch):
    # .env.example має порожній SYNTHESIS_LLM_PROVIDER= — це не "" як
    # провайдер, а "нічого не задано".
    monkeypatch.setenv("SYNTHESIS_LLM_PROVIDER", "")
    assert resolve_provider() == "deepseek"


def test_call_llm_uses_route_provider(monkeypatch):
    """Маршрут за призначенням, не одне глобальне значення
    (config.py:LLM_ROUTES, 2026-10-05)."""
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    captured = {}

    def fake_call_deepseek(prompt, api_key, system_prompt=None, **kwargs):
        captured["prompt"] = prompt
        captured["system_prompt"] = system_prompt
        return json.dumps(VALID_RESPONSE)

    monkeypatch.setattr(llm_common, "call_deepseek", fake_call_deepseek)

    result = call_llm("some prompt", "system", purpose="news_relevance_filter")

    assert result.text == json.dumps(VALID_RESPONSE)
    assert result.provider == "deepseek"
    assert result.fallback_from is None
    assert captured == {"prompt": "some prompt", "system_prompt": "system"}


def test_forecast_route_stays_on_the_cheapest_model(monkeypatch):
    """Рішення користувача 2026-10-05 ПІСЛЯ трьох бектестів: прогноз
    на DeepSeek, не на Opus 5.

    Спершу прогноз маршрутизували на Opus ("дорожча модель на
    найважливіше завдання"), але три прогони на тих самих 19
    показниках × 8 точках це не підтвердили: DeepSeek 8 перемог/4
    поразки, Opus 7/7, Sonnet 6/5 — при десятикратній різниці в ціні
    (docs/archive/plan-journal-2026-10-11.md Фаза 2). Цей тест стереже саме рішення, а не реалізацію:
    якщо хтось поверне дорогу модель без нового бектесту, він
    впаде."""
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    route = resolve_route("metric_forecast")
    assert route.provider == "deepseek"


def test_backtest_shares_the_forecast_route(monkeypatch):
    """"Бектест робимо на тому, на чому буде працювати" — інакше
    міряли б не те, що потім піде в Telegram."""
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    assert resolve_route("metric_forecast_backtest") == resolve_route("metric_forecast")


def test_synthesis_routes_go_to_sonnet(monkeypatch):
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    for purpose in ("market_context_synthesis", "news_price_synthesis", "calendar_outlook"):
        route = resolve_route(purpose)
        assert route.provider == "anthropic"
        assert route.model == config.ANTHROPIC_MODEL_SYNTHESIS


def test_bulk_routes_stay_on_deepseek(monkeypatch):
    """Механіка (найбільше викликів) лишається дешевою — інакше
    фільтр релевантності коштував би дорожче за всі висновки разом."""
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    for purpose in ("news_relevance_filter", "news_consolidation", "candidate_discovery"):
        assert resolve_route(purpose).provider == "deepseek"


def test_env_override_wins_over_every_route(monkeypatch):
    """Аварійний важіль: один env перекриває ВСІ маршрути (скінчився
    бюджет / провайдер лежить)."""
    monkeypatch.setenv("SYNTHESIS_LLM_PROVIDER", "deepseek")
    assert resolve_route("metric_forecast").provider == "deepseek"


def test_synthesis_route_is_the_anthropic_one(monkeypatch):
    """Anthropic лишається там, де якість не міряється MAE — текстові
    висновки, які читає людина (рішення 2026-10-05)."""
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    route = resolve_route("market_context_synthesis")
    assert route.provider == "anthropic"
    assert route.model == config.ANTHROPIC_MODEL_SYNTHESIS


def test_unknown_purpose_falls_back_to_default_route(monkeypatch):
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    route = resolve_route("щось_чого_немає_в_мапі")
    assert route.provider == config.LLM_ROUTE_DEFAULT["provider"]


# ============================================================
# Резерв провайдера
# ============================================================
# Пряме застосування уроку 2026-10-05 (потоки новин стояли тижнями на
# одному джерелі): якщо Anthropic недоступний, прогін не має вмирати —
# але й не має вдавати, що все гаразд.


def test_anthropic_failure_falls_back_to_deepseek(monkeypatch):
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek")

    def boom(*args, **kwargs):
        raise requests.exceptions.ConnectionError("Anthropic недоступний")

    monkeypatch.setattr(llm_common, "call_anthropic", boom)
    monkeypatch.setattr(
        llm_common, "call_deepseek",
        lambda prompt, api_key, system_prompt=None, **kwargs: json.dumps(VALID_RESPONSE),
    )

    # Будь-яке призначення, маршрутизоване на Anthropic (прогноз з
    # 2026-10-05 на DeepSeek — див. тест маршруту вище).
    result = call_llm("prompt", "system", purpose="market_context_synthesis")

    assert result.text == json.dumps(VALID_RESPONSE)
    assert result.provider == "deepseek"
    # Підміна мусить бути ВИДИМОЮ — інакше "працює на дешевшій моделі"
    # виглядало б як норма (саме так тижнями не помічався мертвий GDELT).
    assert result.fallback_from == f"anthropic/{config.ANTHROPIC_MODEL_SYNTHESIS}"


def test_both_providers_down_raises_llm_error(monkeypatch):
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek")

    def boom(*args, **kwargs):
        raise requests.exceptions.ConnectionError("лежить")

    monkeypatch.setattr(llm_common, "call_anthropic", boom)
    monkeypatch.setattr(llm_common, "call_deepseek", boom)

    with pytest.raises(LlmError):
        call_llm("prompt", "system", purpose="market_context_synthesis")


def test_deepseek_failure_does_not_retry_itself(monkeypatch):
    """Повторювати DeepSeek на DeepSeek безглуздо — маршрут уже
    найдешевший, це була б лише подвійна затримка."""
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek")
    calls = []

    def boom(prompt, api_key, system_prompt=None, **kwargs):
        calls.append(1)
        raise requests.exceptions.ConnectionError("лежить")

    monkeypatch.setattr(llm_common, "call_deepseek", boom)

    with pytest.raises(LlmError):
        call_llm("prompt", "system", purpose="news_relevance_filter")
    assert len(calls) == 1


def test_require_api_key_returns_key(monkeypatch):
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    assert require_api_key("news_relevance_filter") == "fake-key"


def test_require_api_key_exits_without_key(monkeypatch):
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        require_api_key("news_relevance_filter")


def test_require_api_key_checks_the_purposes_provider(monkeypatch):
    """Ключ DeepSeek не рятує маршрут на Anthropic — рання помилка
    краща за падіння посеред прогону."""
    monkeypatch.delenv("SYNTHESIS_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        require_api_key("market_context_synthesis")


def test_require_api_key_exits_on_unsupported_provider(monkeypatch):
    monkeypatch.setenv("SYNTHESIS_LLM_PROVIDER", "неіснуючий")
    with pytest.raises(SystemExit):
        require_api_key("metric_forecast")


# ============================================================
# Markdown-огорожа навколо JSON
# ============================================================
# Живий кейс першого виклику Anthropic (2026-10-05): Opus 5 віддав
# коректний JSON, але в ```json-огорожі, і розбір упав на першому
# символі. DeepSeek так не робив — він викликається з
# response_format={"type":"json_object"}, якого в Anthropic немає.


def test_parse_json_object_accepts_fenced_json():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_object_accepts_fence_without_language():
    assert parse_json_object('```\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_object_accepts_unclosed_fence():
    """Модель може обрізати закривну огорожу на межі max_tokens —
    відповідь усе одно придатна."""
    assert parse_json_object('```json\n{"a": 1}') == {"a": 1}


def test_parse_json_object_still_accepts_bare_json():
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_json_object_still_rejects_garbage():
    with pytest.raises(SynthesisResponseError):
        parse_json_object("ось вам текст без json")
