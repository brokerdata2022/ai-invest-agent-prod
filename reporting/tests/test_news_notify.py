from news_notify import MAX_MESSAGE_CHARS, batch_messages, format_item


def _row(**overrides):
    row = {
        "asset_id": "btc",
        "summary": "Bitcoin перевищив 84 000 доларів, наблизившись до найвищого рівня за 8 місяців.",
        "direction": "up",
        "confidence": 0.8,
        "source_count": 3,
        "source_urls": ["https://a.com/1", "https://b.com/1", "https://c.com/1"],
    }
    row.update(overrides)
    return row


def test_format_item_includes_core_fields():
    text = format_item(_row())
    assert "btc" in text
    assert "Bitcoin перевищив 84 000 доларів" in text
    assert "https://a.com/1" in text
    assert "🟢" in text  # direction=up


def test_format_item_shows_source_count_when_multiple():
    assert "(3 джерел)" in format_item(_row(source_count=3))


def test_format_item_omits_source_count_note_for_single_source():
    text = format_item(_row(source_count=1, source_urls=["https://a.com/1"]))
    assert "джерел" not in text


def test_format_item_unknown_asset_id():
    assert "[—]" in format_item(_row(asset_id=None))


def test_format_item_unknown_direction_falls_back_to_question_mark():
    assert "❓" in format_item(_row(direction="something_unexpected"))


def test_format_item_caps_urls_shown():
    text = format_item(_row(source_urls=[f"https://x.com/{i}" for i in range(10)]))
    assert text.count("https://x.com/") == 3


def test_batch_messages_empty_returns_no_messages():
    assert batch_messages([]) == []


def test_batch_messages_packs_several_items_into_one_message():
    # Регресія 2026-09-28 (баг №2): "одна новина — одне повідомлення"
    # заспамило користувача десятками повідомлень підряд.
    rows = [_row(summary=f"Новина {i}") for i in range(5)]
    messages = batch_messages(rows)
    assert len(messages) == 1
    assert "Новина 0" in messages[0]
    assert "Новина 4" in messages[0]
    assert messages[0].startswith("<b>📰 Важливі новини (5):</b>")


def test_batch_messages_splits_when_exceeding_char_limit():
    # Регресія 2026-09-28 (баг №1): одне гігантське повідомлення
    # перевищило ліміт Telegram (4096) → 400 Bad Request.
    rows = [_row(summary="X" * 1000) for _ in range(5)]
    messages = batch_messages(rows)
    assert len(messages) > 1
    for msg in messages:
        assert len(msg) <= MAX_MESSAGE_CHARS + 1500  # запас на один "останній" елемент понад межу

    all_text = "\n\n".join(messages)
    for i in range(5):
        assert all_text.count("X" * 1000) == 5


def test_batch_messages_single_oversized_item_still_gets_its_own_message():
    """Одна новина, більша за max_chars сама по собі — не застрягає в
    нескінченному циклі, просто йде окремим (надто довгим) повідомленням."""
    rows = [_row(summary="Y" * 5000)]
    messages = batch_messages(rows, max_chars=100)
    assert len(messages) == 1
    assert "Y" * 5000 in messages[0]


class _FakeCursor:
    def __init__(self, rows=()):
        self.rows = rows
        self.description = [("id",)]
        self.executed = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params):
        self.executed = (query, params)

    def fetchall(self):
        return self.rows


class _FakeConn:
    def __init__(self):
        self.cursor_obj = _FakeCursor()

    def cursor(self):
        return self.cursor_obj


def test_fetch_top_unnotified_explicit_confidence_overrides_per_stream(monkeypatch):
    import news_notify

    conn = _FakeConn()
    news_notify.fetch_top_unnotified(conn, stream=None, min_confidence=0.9, limit=5)
    query, params = conn.cursor_obj.executed
    assert "CASE stream" not in query
    assert params[0] == 0.9


def test_fetch_top_unnotified_single_stream_uses_its_own_threshold():
    import news_notify

    conn = _FakeConn()
    news_notify.fetch_top_unnotified(conn, stream="geopolitical", min_confidence=None, limit=5)
    query, params = conn.cursor_obj.executed
    assert "CASE stream" not in query
    assert params[0] == news_notify.MIN_CONFIDENCE_BY_STREAM["geopolitical"]
    assert "geopolitical" in params


def test_fetch_top_unnotified_all_streams_uses_case_expression():
    import news_notify

    conn = _FakeConn()
    news_notify.fetch_top_unnotified(conn, stream=None, min_confidence=None, limit=5)
    query, params = conn.cursor_obj.executed
    assert "CASE stream" in query
    assert tuple(params[:3]) == (
        news_notify.MIN_CONFIDENCE_BY_STREAM["watchlist"],
        news_notify.MIN_CONFIDENCE_BY_STREAM["general"],
        news_notify.MIN_CONFIDENCE_BY_STREAM["geopolitical"],
    )


def test_geopolitical_threshold_is_stricter_than_watchlist_and_general():
    # Regression 2026-09-28: geopolitical (hurricane/military-strike
    # items без явного економічного наслідку) сідало на 0.6-0.7 так
    # само часто, як реальний ринковий контент watchlist/general.
    import news_notify

    assert news_notify.MIN_CONFIDENCE_BY_STREAM["geopolitical"] > news_notify.MIN_CONFIDENCE_BY_STREAM["watchlist"]
    assert news_notify.MIN_CONFIDENCE_BY_STREAM["geopolitical"] > news_notify.MIN_CONFIDENCE_BY_STREAM["general"]


def _mk_row(id, stream, confidence, source_count=1):
    return {"id": id, "stream": stream, "confidence": confidence, "source_count": source_count}


def test_fetch_prioritized_reserves_watchlist_slots(monkeypatch):
    import news_notify

    def fake_fetch_top(conn, stream=None, min_confidence=None, limit=8):
        if stream == "watchlist":
            return [_mk_row(1, "watchlist", 0.6), _mk_row(2, "watchlist", 0.6)]
        # Комбінований пул — generic-контент з вищим confidence, який
        # у плоскому сортуванні забив би всі місця (regression 2026-09-28).
        return [
            _mk_row(10, "general", 0.9),
            _mk_row(11, "general", 0.9),
            _mk_row(1, "watchlist", 0.6),
            _mk_row(2, "watchlist", 0.6),
            _mk_row(12, "general", 0.8),
        ]

    monkeypatch.setattr(news_notify, "fetch_top_unnotified", fake_fetch_top)

    rows = news_notify.fetch_prioritized_unnotified(conn=None, limit=4, watchlist_min_slots=3)

    watchlist_count = sum(1 for r in rows if r["stream"] == "watchlist")
    assert watchlist_count == 2  # обидва доступні watchlist-записи присутні
    assert len(rows) == 4


def test_fetch_prioritized_fills_rest_from_any_stream_when_watchlist_thin(monkeypatch):
    import news_notify

    def fake_fetch_top(conn, stream=None, min_confidence=None, limit=8):
        if stream == "watchlist":
            return [_mk_row(1, "watchlist", 0.6)]
        return [_mk_row(1, "watchlist", 0.6), _mk_row(10, "general", 0.9), _mk_row(20, "geopolitical", 0.8)]

    monkeypatch.setattr(news_notify, "fetch_top_unnotified", fake_fetch_top)

    rows = news_notify.fetch_prioritized_unnotified(conn=None, limit=3, watchlist_min_slots=3)

    ids = {r["id"] for r in rows}
    assert ids == {1, 10, 20}


def test_fetch_prioritized_caps_at_limit_even_with_enough_watchlist(monkeypatch):
    import news_notify

    def fake_fetch_top(conn, stream=None, min_confidence=None, limit=8):
        return [_mk_row(i, "watchlist", 0.6) for i in range(5)]

    monkeypatch.setattr(news_notify, "fetch_top_unnotified", fake_fetch_top)

    rows = news_notify.fetch_prioritized_unnotified(conn=None, limit=3, watchlist_min_slots=3)
    assert len(rows) == 3
