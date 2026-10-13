"""
Тести GdeltAdapter на збережених прикладах відповіді API — жодних
реальних мережевих викликів (див. .claude/skills/add-data-source, п.3).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import config
from news.gdelt_adapter import GdeltAdapter, GdeltError, validate_gdelt_term

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def articles_response():
    with open(FIXTURES_DIR / "gdelt_articles_response.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def adapter():
    return GdeltAdapter(stream="watchlist", query='"Nvidia" OR "Federal Reserve"')


def test_unknown_stream_rejected():
    with pytest.raises(ValueError):
        GdeltAdapter(stream="not_a_real_stream", query="test")


def test_empty_query_rejected():
    with pytest.raises(ValueError):
        GdeltAdapter(stream="watchlist", query="")


def test_normalize_produces_expected_records(adapter, articles_response):
    records = adapter.normalize(articles_response)

    # Третя стаття без title, четверта з некоректним seendate — обидві пропущені.
    assert len(records) == 2

    first = records[0]
    assert first.source == "gdelt"
    assert first.stream == "watchlist"
    assert first.external_id == "https://example-news.com/articles/fed-rate-decision-2026-09-25"
    assert first.title == "Fed holds rates steady, signals caution on inflation"
    assert first.url == "https://example-news.com/articles/fed-rate-decision-2026-09-25"
    assert first.published_at == datetime(2026, 9, 25, 14, 0, 0, tzinfo=timezone.utc)
    assert first.raw_payload["domain"] == "example-news.com"


def test_normalize_skips_articles_missing_required_fields(adapter, articles_response):
    records = adapter.normalize(articles_response)
    urls = {r.url for r in records}
    assert "https://example-wire.com/no-title-here" not in urls
    assert "https://example-wire.com/bad-date" not in urls


def test_normalize_sets_fetched_at_close_to_now(adapter, articles_response):
    records = adapter.normalize(articles_response)
    now = datetime.now(timezone.utc)
    for record in records:
        assert (now - record.fetched_at).total_seconds() < 5


def test_stream_is_stored_on_every_record():
    adapter = GdeltAdapter(stream="geopolitical", query="sanctions")
    response = {
        "articles": [
            {
                "url": "https://example.com/a",
                "title": "New sanctions announced",
                "seendate": "20260925T100000Z",
            }
        ]
    }
    records = adapter.normalize(response)
    assert records[0].stream == "geopolitical"


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class _FakeSession:
    """Повертає по одній заготовленій відповіді на кожен .get() виклик."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.last_params = None

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        self.last_params = params
        return self._responses.pop(0)


def test_fetch_retries_on_429_then_succeeds():
    session = _FakeSession(
        [_FakeResponse(429), _FakeResponse(429), _FakeResponse(200, {"articles": []})]
    )
    sleeps = []
    adapter = GdeltAdapter(
        stream="watchlist", query="bitcoin", session=session, sleep=sleeps.append
    )

    result = adapter.fetch()

    assert result == {"articles": []}
    assert session.calls == 3
    # Затримки — з конфіга (config.py розділ 8), не літерали: 2026-10-05
    # їх подовжено з (5,10,20) до (30,120), бо швидкий повтор на 429
    # лише глибше заганяв IP у штрафний стан GDELT.
    assert sleeps == list(config.GDELT_RETRY_DELAYS)


def test_fetch_raises_after_exhausting_retries():
    # 429 на кожну спробу, включно з останньою — після вичерпання
    # retry-бюджету має піднятись помилка, не тихо повернути None.
    session = _FakeSession([_FakeResponse(429)] * 4)
    adapter = GdeltAdapter(
        stream="watchlist", query="bitcoin", session=session, sleep=lambda _: None
    )

    with pytest.raises(RuntimeError):
        adapter.fetch()


def test_fetch_retries_on_non_json_response_then_succeeds():
    # GDELT інколи віддає 200 з порожнім/не-JSON тілом (напр. занадто
    # довгий query) — той самий retry-бюджет, що й для 429.
    session = _FakeSession(
        [
            _FakeResponse(200, payload=None, text=""),
            _FakeResponse(200, {"articles": []}),
        ]
    )
    sleeps = []
    adapter = GdeltAdapter(
        stream="watchlist", query="bitcoin", session=session, sleep=sleeps.append
    )

    result = adapter.fetch()

    assert result == {"articles": []}
    assert sleeps == [config.GDELT_RETRY_DELAYS[0]]


def test_fetch_raises_gdelt_error_after_exhausting_retries_on_non_json():
    session = _FakeSession([_FakeResponse(200, payload=None, text="")] * 4)
    adapter = GdeltAdapter(
        stream="watchlist", query="bitcoin", session=session, sleep=lambda _: None
    )

    with pytest.raises(GdeltError):
        adapter.fetch()


def test_fetch_defaults_to_1d_timespan():
    # Regression 2026-09-26: без обмеження часу GDELT віддавав місяцями
    # старі статті (стаття про ставку з липня в результатах у вересні).
    # Звужено з "3d" до "1d" 2026-09-28 (рішення користувача: лише
    # статті за останні 24г, common/news_db.py:MAX_ARTICLE_AGE_HOURS).
    session = _FakeSession([_FakeResponse(200, {"articles": []})])
    adapter = GdeltAdapter(stream="watchlist", query="bitcoin", session=session)

    adapter.fetch()

    assert session.last_params["timespan"] == "1d"


def test_fetch_accepts_custom_timespan():
    session = _FakeSession([_FakeResponse(200, {"articles": []})])
    adapter = GdeltAdapter(stream="watchlist", query="bitcoin", session=session)

    adapter.fetch(timespan="1w")

    assert session.last_params["timespan"] == "1w"


# --- validate_gdelt_term() (2026-10-04, живий кейс "BNB" — watchlist_add) ---

def test_validate_gdelt_term_true_on_valid_json():
    session = _FakeSession([_FakeResponse(200, {"articles": []})])
    assert validate_gdelt_term("gold price", session=session) is True


def test_validate_gdelt_term_false_on_non_json_rejection():
    # Той самий "Uber"-урок — GDELT віддає 200 з текстом "The specified
    # phrase is too short.", не валідний JSON.
    session = _FakeSession([_FakeResponse(200, payload=None, text="The specified phrase is too short.\n")])
    assert validate_gdelt_term("BNB", session=session) is False


class _RaisingSession:
    """.get() одразу кидає мережеву помилку — на відміну від
    _FakeSession/_FakeResponse вище (raise_for_status() там — плоский
    RuntimeError для тесту fetch(), не requests.exceptions.*), тут
    навмисно справжній тип з requests, який validate_gdelt_term() і
    ловить."""

    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        import requests

        raise requests.exceptions.ConnectionError("boom")


def test_validate_gdelt_term_none_on_request_error():
    """2026-10-06: мережева помилка це "не змогли перевірити" (None), а
    НЕ "термін поганий" (False). Плутати їх означало назавжди лишити
    актив без новин через одну транзієнтну помилку."""
    session = _RaisingSession()
    assert validate_gdelt_term("x", session=session) is None


def test_validate_gdelt_term_does_not_retry():
    # На відміну від GdeltAdapter.fetch() — тут НЕМАЄ retry: відмова
    # детермінована (чи то "too short", чи мережева помилка), повтор
    # нічого не змінить.
    session = _RaisingSession()
    validate_gdelt_term("x", session=session)
    assert session.calls == 1


# ============================================================
# Глобальний обмежувач швидкості (pacer)
# ============================================================
# Жива причина (2026-10-05, docs/decisions.md): три джоби збору новин —
# три ОКРЕМІ процеси, тож пауза в коді одного нічого не знала про
# запити двох інших. GDELT почав відповідати 429 на першому ж запиті.


def test_pacer_called_before_every_request_including_retries():
    """Якщо обмежувач не викликається на ПОВТОРАХ, retry-цикл обходить
    ліміт — це й був механізм самопідсилення збою."""
    session = _FakeSession(
        [_FakeResponse(429), _FakeResponse(429), _FakeResponse(200, {"articles": []})]
    )
    paced = []
    adapter = GdeltAdapter(
        stream="watchlist", query="bitcoin", session=session,
        sleep=lambda _: None, pacer=lambda: paced.append(1),
    )

    adapter.fetch()

    assert session.calls == 3
    assert len(paced) == 3


def test_pacer_is_optional():
    """Без pacer адаптер працює (ручний одноразовий запуск, тести) —
    але джоби його передають обовʼязково."""
    session = _FakeSession([_FakeResponse(200, {"articles": []})])
    adapter = GdeltAdapter(stream="watchlist", query="bitcoin", session=session)

    assert adapter.fetch() == {"articles": []}


# ============================================================
# Мʼякий троттл GDELT: HTTP 200 з тілом `{}`
# ============================================================
# Живо виявлено 2026-10-05 (docs/decisions.md): саме через це потоки
# `general`/`watchlist` писали в лог "Отримано 0 статей", скрипт
# виходив з кодом 0, а runner.py вважав прогін УСПІШНИМ — тижні тихого
# втрачання даних без жодного сигналу.


def test_empty_object_is_treated_as_throttle_and_retried():
    session = _FakeSession(
        [_FakeResponse(200, {}), _FakeResponse(200, {"articles": [{"url": "u"}]})]
    )
    sleeps = []
    adapter = GdeltAdapter(
        stream="watchlist", query="bitcoin", session=session, sleep=sleeps.append
    )

    result = adapter.fetch()

    assert result == {"articles": [{"url": "u"}]}
    assert session.calls == 2
    assert sleeps == [config.GDELT_RETRY_DELAYS[0]]


def test_empty_object_raises_after_retries_not_silent_zero():
    """Головне: НЕ повернути "0 статей" тихо. Виняток піднімає скрипт
    із кодом 1, і runner.py бачить провал."""
    session = _FakeSession([_FakeResponse(200, {})] * 3)
    adapter = GdeltAdapter(
        stream="watchlist", query="bitcoin", session=session, sleep=lambda _: None
    )

    with pytest.raises(GdeltError) as exc:
        adapter.fetch()
    assert "articles" in str(exc.value)


def test_genuine_empty_result_is_not_a_throttle():
    """Справжнє "немає збігів" — це {"articles": []}: ключ Є, список
    порожній. Воно мусить проходити як успіх, інакше тихий спокійний
    день на ринку виглядав би як збій."""
    session = _FakeSession([_FakeResponse(200, {"articles": []})])
    adapter = GdeltAdapter(stream="watchlist", query="bitcoin", session=session)

    assert adapter.fetch() == {"articles": []}


def test_validate_term_returns_none_on_throttle():
    """Мʼякий троттл — "не змогли перевірити" (None), не відмова.
    Хибне True пустило б НЕПЕРЕВІРЕНИЙ термін у спільний
    watchlist-запит (один поганий OR-терм ламає новини для ВСІХ
    активів, живий кейс "BNB"), а хибне False назавжди лишило б актив
    без новин — тому третій стан (2026-10-06)."""
    session = _FakeSession([_FakeResponse(200, {})])
    assert validate_gdelt_term("bitcoin", session=session) is None


def test_validate_term_returns_false_when_gdelt_rejects_the_phrase():
    """Відхилення фрази GDELT віддає НЕ-JSON тілом ("The specified
    phrase is too short") — це остаточна відмова, повторювати
    безглуздо."""
    session = _FakeSession([_FakeResponse(200, payload=None, text="phrase is too short")])
    assert validate_gdelt_term("bnb", session=session) is False
