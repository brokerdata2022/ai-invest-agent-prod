"""
Тести _common.py:mark_notified() — сама логіка (порожній список/білий
список таблиць), без реальної БД (фейковий conn/cursor); та
escape_html()/bold()/link() — HTML-хелпери для Telegram
parse_mode="HTML" (2026-10-03)."""

import pytest

from _common import bold, escape_html, link, mark_notified


class _FakeCursor:
    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params):
        self.executed.append((query, params))


class _FakeConn:
    def __init__(self):
        self.cursor_obj = _FakeCursor()
        self.committed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True


def test_mark_notified_noop_on_empty_ids():
    conn = _FakeConn()
    mark_notified(conn, "news_analysis", [])
    assert conn.cursor_obj.executed == []
    assert conn.committed is False


def test_mark_notified_rejects_unknown_table():
    conn = _FakeConn()
    with pytest.raises(ValueError):
        mark_notified(conn, "raw_observations", [1, 2])
    assert conn.cursor_obj.executed == []


@pytest.mark.parametrize(
    "table",
    [
        "news_analysis", "news_synthesis", "market_synthesis", "candidate_assets",
        "news_consolidated", "screening_results", "crypto_screening_candidates",
        "crypto_long_candidates", "calendar_outlook", "metric_forecasts",
        "trading_list", "fundamental_analysis",
    ],
)
def test_mark_notified_executes_update_for_known_tables(table):
    conn = _FakeConn()
    mark_notified(conn, table, [1, 2, 3])

    assert conn.committed is True
    assert len(conn.cursor_obj.executed) == 1
    query, params = conn.cursor_obj.executed[0]
    assert table in query
    assert "notified_at = now()" in query
    assert params == ([1, 2, 3],)


def test_escape_html_escapes_reserved_characters():
    assert escape_html("<script>&") == "&lt;script&gt;&amp;"


def test_escape_html_leaves_quotes_alone():
    # quote=False — лапки в ТЕКСТІ повідомлення не ескейпляться
    # (тільки href в link() нижче цього потребує).
    assert escape_html('He said "hi"') == 'He said "hi"'


def test_escape_html_coerces_non_string_values():
    assert escape_html(42) == "42"


def test_bold_wraps_and_escapes():
    assert bold("CPI & Core CPI") == "<b>CPI &amp; Core CPI</b>"


def test_link_wraps_text_and_href_escaped():
    text = link("Джерело 1", "https://example.com/a?x=1&y=2")
    assert text == '<a href="https://example.com/a?x=1&amp;y=2">Джерело 1</a>'


# --- час у поясі користувача ------------------------------------------
# Живий фідбек 2026-10-05: у Telegram ішло
# "2026-10-08 12:30:00+00:00" — scheduled_at це TIMESTAMPTZ, psycopg2
# віддає його в UTC, а f-string друкував як є.


def test_format_local_dt_converts_utc_to_display_timezone():
    from datetime import datetime, timezone

    import config
    from _common import format_local_dt

    # 12:30 UTC = 15:30 Києва (EEST, UTC+3) у жовтні до переходу.
    utc = datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)
    assert config.DISPLAY_TIMEZONE == "Europe/Kyiv"
    assert format_local_dt(utc) == "08.10 15:30"


def test_format_local_dt_treats_naive_as_utc():
    """Naive-значення — саме так його віддає БД без tz у сесії.
    Видавати його за локальний означало б зсунути час ТИХО."""
    from datetime import datetime

    from _common import format_local_dt

    assert format_local_dt(datetime(2026, 10, 8, 12, 30)) == "08.10 15:30"


def test_format_local_dt_respects_winter_time():
    """Після останньої неділі жовтня Київ на EET (UTC+2) — зсув має
    змінитись сам, без правок у коді."""
    from datetime import datetime, timezone

    from _common import format_local_dt

    utc = datetime(2026, 12, 8, 12, 30, tzinfo=timezone.utc)
    assert format_local_dt(utc) == "08.12 14:30"


def test_format_local_dt_passes_through_non_datetime():
    """Notify-скрипт не повинен падати через несподіваний тип у полі,
    яке лише показується."""
    from _common import format_local_dt

    assert format_local_dt("уже рядок") == "уже рядок"
    assert format_local_dt(None) == ""


def test_format_local_dt_custom_format():
    from datetime import datetime, timezone

    from _common import format_local_dt

    utc = datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)
    assert format_local_dt(utc, "%H:%M") == "15:30"
