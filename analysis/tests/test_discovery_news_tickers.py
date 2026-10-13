"""
Новинний детектор новачків (`discovery/news_tickers.py`) — єдиний з
трьох, що викликає LLM, і єдиний, де LLM може збрехати.

Тому тести тут саме про межу довіри: тикер приймається ЛИШЕ якщо
Twelve Data віддала реальний ряд цін, а напрямок береться з ЦІНИ, не
зі слів LLM.
"""

from decimal import Decimal

import pytest

from discovery import news_tickers
from news_analysis.aggregate import NewsCluster


class _FakeConn:
    def commit(self):
        pass


def _cluster(title="Acme виграла контракт", source_count=4):
    return NewsCluster(
        representative_title=title,
        source_count=source_count,
        asset_ids=[],
        dominant_direction="up",
        direction_counts={},
        summaries=["s"],
        urls=["http://a", "http://b"],
        earliest_published_at=None,
        latest_published_at=None,
    )


class _Record:
    def __init__(self, metric_id, value, observed_at):
        self.metric_id = metric_id
        self.value = value
        self.observed_at = observed_at


def _fake_series(closes, volumes=None):
    records = []
    for day, close in enumerate(closes, start=1):
        records.append(_Record("acme_close", Decimal(close), day))
    for day, volume in enumerate(volumes or [], start=1):
        records.append(_Record("acme_volume", Decimal(volume), day))
    return records


# ============================================================
# Розбір відповіді LLM
# ============================================================


def test_parse_response_accepts_empty_list():
    """Порожній список — нормальний результат, не помилка: у новинах
    може не бути ні однієї конкретної публічної компанії."""
    assert news_tickers.parse_response('{"tickers": []}') == []


def test_parse_response_rejects_missing_field():
    with pytest.raises(news_tickers.NewsTickerResponseError):
        news_tickers.parse_response('{"tickers": [{"ticker": "ACME"}]}')


def test_parse_response_rejects_wrong_shape():
    with pytest.raises(news_tickers.NewsTickerResponseError):
        news_tickers.parse_response('{"candidates": []}')


def test_build_prompt_numbers_stories_for_reference():
    """Номер історії — єдиний спосіб звести кандидата з кластером, з
    якого він узявся (source_refs для аудиту, rule 5)."""
    prompt = news_tickers.build_prompt([_cluster("A"), _cluster("B")], ["AAPL"])
    assert "1. [4 джерел] A" in prompt
    assert "2. [4 джерел] B" in prompt
    assert "AAPL" in prompt


# ============================================================
# Верифікація
# ============================================================


def test_unverified_ticker_is_dropped(monkeypatch):
    """LLM вигадав тикер — Twelve Data його не знає. Кандидат НЕ
    з'являється (той самий гейт, що був і в старій реалізації)."""
    monkeypatch.setattr(news_tickers, "verify_tradable", lambda *a, **k: ([], []))

    found = news_tickers.detect(
        _FakeConn(), [_cluster()], set(),
        [{"ticker": "FAKE", "company_name": "Fake Inc", "story": 1}],
        "key",
    )
    assert found == []


def test_direction_comes_from_price_not_from_llm(monkeypatch):
    """Новина позитивна, а ціна за тиждень впала — напрямок мусить
    бути 'down': перевірити ми можемо лише реакцію ринку."""
    monkeypatch.setattr(
        news_tickers,
        "verify_tradable",
        lambda *a, **k: ([Decimal(x) for x in ["100"] * 20 + ["85"]], []),
    )

    found = news_tickers.detect(
        _FakeConn(), [_cluster()], set(),
        [{"ticker": "ACME", "company_name": "Acme Robotics", "story": 1, "mention": "виграла контракт"}],
        "key",
    )

    assert len(found) == 1
    assert found[0].direction == "down"
    assert found[0].detector == "news_tickers"
    assert found[0].signals["news_source_count"] == 4
    assert found[0].source_refs[0]["title"] == "Acme виграла контракт"


def test_tracked_ticker_is_skipped(monkeypatch):
    called = []
    monkeypatch.setattr(
        news_tickers, "verify_tradable", lambda *a, **k: called.append(1) or ([], [])
    )

    news_tickers.detect(
        _FakeConn(), [_cluster()], {"AAPL"},
        [{"ticker": "AAPL", "company_name": "Apple", "story": 1}],
        "key",
    )
    # Квота Twelve Data не витрачається на те, що ми й так ведемо.
    assert called == []


def test_bad_story_index_does_not_break_run(monkeypatch):
    monkeypatch.setattr(
        news_tickers,
        "verify_tradable",
        lambda *a, **k: ([Decimal(x) for x in ["100"] * 20 + ["130"]], []),
    )

    found = news_tickers.detect(
        _FakeConn(), [_cluster()], set(),
        [{"ticker": "ACME", "company_name": "Acme", "story": 99}],
        "key",
    )
    assert len(found) == 1
    assert found[0].direction == "up"
