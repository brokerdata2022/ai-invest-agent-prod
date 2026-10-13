"""
Тести news_db.py:insert_news_batch()/prune_stale_news() — фільтр
свіжості (MAX_ARTICLE_AGE_HOURS, 2026-09-28, рішення користувача:
raw_news НЕ append-only-вічний, на відміну від raw_observations) +
виняток для fed_rss/ecb_rss/boj_rss (2026-09-29, рішення користувача:
ці 3 джерела публікують рідко, суворе 24г/48г вікно губило їх
повністю — живо підтверджено, fed_rss мав 0 рядків узагалі попри
робочий фід). insert_news() сам (реальний SQL) тут не тестується — те
саме навмисне обмеження, що й решта проєкту (реальна БД не
піднімається в тестах).
"""

from datetime import datetime, timedelta, timezone

import common.news_db as news_db
from common.news_adapter import NewsRecord


def _record(published_at, external_id="a1", source="gdelt"):
    return NewsRecord(
        source=source, external_id=external_id, stream="general",
        title="Заголовок", url="https://example.com/a", published_at=published_at,
        fetched_at=datetime.now(timezone.utc),
    )


def test_insert_news_batch_inserts_fresh_article(monkeypatch):
    inserted_records = []
    monkeypatch.setattr(news_db, "insert_news", lambda conn, record: inserted_records.append(record) or True)

    fresh = _record(datetime.now(timezone.utc) - timedelta(hours=1))
    count = news_db.insert_news_batch(conn=None, records=[fresh])

    assert count == 1
    assert inserted_records == [fresh]


def test_insert_news_batch_skips_stale_article(monkeypatch):
    inserted_records = []
    monkeypatch.setattr(news_db, "insert_news", lambda conn, record: inserted_records.append(record) or True)

    stale = _record(datetime.now(timezone.utc) - timedelta(hours=25))
    count = news_db.insert_news_batch(conn=None, records=[stale])

    assert count == 0
    assert inserted_records == []


def test_insert_news_batch_respects_custom_max_age(monkeypatch):
    monkeypatch.setattr(news_db, "insert_news", lambda conn, record: True)

    borderline = _record(datetime.now(timezone.utc) - timedelta(hours=10))
    assert news_db.insert_news_batch(conn=None, records=[borderline], max_age_hours=6) == 0
    assert news_db.insert_news_batch(conn=None, records=[borderline], max_age_hours=24) == 1


def test_insert_news_batch_mixes_fresh_and_stale(monkeypatch):
    inserted = []
    monkeypatch.setattr(
        news_db, "insert_news",
        lambda conn, record: (inserted.append(record.external_id), True)[1],
    )

    now = datetime.now(timezone.utc)
    records = [
        _record(now - timedelta(hours=1), external_id="fresh"),
        _record(now - timedelta(hours=48), external_id="stale"),
    ]
    count = news_db.insert_news_batch(conn=None, records=records)

    assert count == 1
    assert inserted == ["fresh"]


def test_default_max_age_is_24_hours():
    assert news_db.MAX_ARTICLE_AGE_HOURS == 24


# ---------- EXTENDED_FRESHNESS_SOURCES (2026-09-29) ----------

def test_extended_sources_include_the_three_official_feeds():
    assert news_db.EXTENDED_FRESHNESS_SOURCES == {"fed_rss", "ecb_rss", "boj_rss"}


def test_insert_news_batch_accepts_old_article_from_extended_source(monkeypatch):
    monkeypatch.setattr(news_db, "insert_news", lambda conn, record: True)

    # 3 дні — застаріле для звичайного 24г вікна, свіже для 7-денного.
    article = _record(datetime.now(timezone.utc) - timedelta(days=3), source="fed_rss")
    assert news_db.insert_news_batch(conn=None, records=[article]) == 1


def test_insert_news_batch_still_rejects_ancient_article_from_extended_source(monkeypatch):
    monkeypatch.setattr(news_db, "insert_news", lambda conn, record: True)

    article = _record(datetime.now(timezone.utc) - timedelta(days=10), source="fed_rss")
    assert news_db.insert_news_batch(conn=None, records=[article]) == 0


def test_insert_news_batch_does_not_extend_window_for_ordinary_gdelt_source(monkeypatch):
    monkeypatch.setattr(news_db, "insert_news", lambda conn, record: True)

    # Той самий вік, що вище проходив для fed_rss — для gdelt має відсіятись.
    article = _record(datetime.now(timezone.utc) - timedelta(days=3), source="gdelt")
    assert news_db.insert_news_batch(conn=None, records=[article]) == 0


def test_insert_news_batch_applies_per_record_source_in_mixed_batch(monkeypatch):
    """Один виклик може мати записи з різних джерел одночасно (напр.
    майбутнє об'єднання колекторів) — кожен фільтрується за СВОЇМ
    джерелом, не одним числом на весь батч."""
    inserted = []
    monkeypatch.setattr(
        news_db, "insert_news",
        lambda conn, record: (inserted.append(record.external_id), True)[1],
    )

    now = datetime.now(timezone.utc)
    records = [
        _record(now - timedelta(days=3), external_id="old_fed", source="fed_rss"),
        _record(now - timedelta(days=3), external_id="old_gdelt", source="gdelt"),
    ]
    count = news_db.insert_news_batch(conn=None, records=records)

    assert count == 1
    assert inserted == ["old_fed"]


def test_explicit_max_age_hours_overrides_extended_sources_too(monkeypatch):
    monkeypatch.setattr(news_db, "insert_news", lambda conn, record: True)

    article = _record(datetime.now(timezone.utc) - timedelta(days=3), source="fed_rss")
    # Явний max_age_hours=24 переважає навіть для fed_rss.
    assert news_db.insert_news_batch(conn=None, records=[article], max_age_hours=24) == 0


class _FakeCursor:
    def __init__(self, rowcount=0):
        self.rowcount = rowcount
        self.executed = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params):
        self.executed = (query, params)


class _FakeConn:
    def __init__(self, rowcount=0):
        self.cursor_obj = _FakeCursor(rowcount)
        self.committed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True


def test_prune_stale_news_deletes_and_commits():
    conn = _FakeConn(rowcount=7)
    deleted = news_db.prune_stale_news(conn)

    assert deleted == 7
    assert conn.committed is True
    query, params = conn.cursor_obj.executed
    assert "DELETE FROM raw_news" in query
    assert "fed_rss" in query  # виняток вшитий у сам SQL, не параметр
    assert params == (news_db.RETENTION_HOURS, news_db.EXTENDED_RETENTION_HOURS)


def test_prune_stale_news_respects_custom_max_age():
    conn = _FakeConn(rowcount=0)
    news_db.prune_stale_news(conn, max_age_hours=72, extended_max_age_hours=200)
    _, params = conn.cursor_obj.executed
    assert params == (72, 200)


def test_retention_is_longer_than_collection_window():
    # RETENTION (скільки тримаємо) має бути >= MAX_ARTICLE_AGE_HOURS
    # (скільки збираємо) — інакше щойно зібрана стаття могла б одразу
    # опинитись поза вікном зберігання. Те саме — для розширеної пари.
    assert news_db.RETENTION_HOURS >= news_db.MAX_ARTICLE_AGE_HOURS
    assert news_db.RETENTION_HOURS == 48
    assert news_db.EXTENDED_RETENTION_HOURS >= news_db.EXTENDED_MAX_ARTICLE_AGE_HOURS
    assert news_db.EXTENDED_RETENTION_HOURS == 24 * 7
