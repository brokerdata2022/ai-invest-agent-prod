"""
Тести RssAdapter на збережених прикладах відповіді (реальна структура
Fed/ECB/BOJ RSS, перевірена живими запитами 2026-09-26) — жодних
реальних мережевих викликів (див. .claude/skills/add-data-source, п.3).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from news.rss_adapter import RssAdapter, parse_pub_date

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


@pytest.fixture
def fed_response():
    return _fixture("fed_rss_response.xml")


@pytest.fixture
def ecb_response():
    return _fixture("ecb_rss_response.xml")


@pytest.fixture
def fed_adapter():
    return RssAdapter(
        source="fed_rss",
        stream="geopolitical",
        feed_url="https://www.federalreserve.gov/feeds/press_all.xml",
    )


@pytest.fixture
def ecb_adapter():
    return RssAdapter(
        source="ecb_rss",
        stream="geopolitical",
        feed_url="https://www.ecb.europa.eu/rss/press.xml",
    )


@pytest.fixture
def boj_response():
    return _fixture("boj_rss_response.xml")


@pytest.fixture
def boj_adapter():
    return RssAdapter(
        source="boj_rss",
        stream="geopolitical",
        feed_url="https://www.boj.or.jp/en/rss/whatsnew.xml",
    )


def test_unknown_stream_rejected():
    with pytest.raises(ValueError):
        RssAdapter(source="fed_rss", stream="not_a_real_stream", feed_url="https://example.com/rss")


def test_empty_feed_url_rejected():
    with pytest.raises(ValueError):
        RssAdapter(source="fed_rss", stream="geopolitical", feed_url="")


def test_normalize_fed_produces_expected_records(fed_adapter, fed_response):
    records = fed_adapter.normalize(fed_response)

    # Третій item без pubDate -- має бути пропущений.
    assert len(records) == 2

    first = records[0]
    assert first.source == "fed_rss"
    assert first.stream == "geopolitical"
    assert first.title == "Federal Reserve Board announces approval of application by Peoples Bancorp Inc."
    assert first.url == "https://www.federalreserve.gov/newsevents/pressreleases/orders20260925a.htm"
    assert first.external_id == first.url
    assert first.published_at == datetime(2026, 9, 25, 20, 30, 0, tzinfo=timezone.utc)
    assert first.raw_payload["description"] == (
        "Federal Reserve Board announces approval of application by Peoples Bancorp Inc."
    )


def test_normalize_skips_item_missing_pubdate(fed_adapter, fed_response):
    records = fed_adapter.normalize(fed_response)
    urls = {r.url for r in records}
    assert "https://www.federalreserve.gov/newsevents/pressreleases/broken.htm" not in urls


def test_normalize_ecb_handles_missing_description(ecb_adapter, ecb_response):
    # ECB-фід не дає <description> взагалі -- має нормалізуватись без
    # падіння, з description=None у raw_payload.
    records = ecb_adapter.normalize(ecb_response)
    assert len(records) == 2
    assert records[0].raw_payload["description"] is None


def test_normalize_ecb_parses_offset_pubdate(ecb_adapter, ecb_response):
    records = ecb_adapter.normalize(ecb_response)
    # +0200 -- не GMT/UTC як у Fed, той самий парсер має впоратись.
    assert records[0].published_at == datetime(
        2026, 9, 24, 14, 0, 0, tzinfo=timezone(timedelta(hours=2))
    )


def test_normalize_boj_produces_expected_records(boj_adapter, boj_response):
    records = boj_adapter.normalize(boj_response)
    assert len(records) == 2

    first = records[0]
    assert first.source == "boj_rss"
    assert first.title == "Conduct of Funds-Supplying Operations against Pooled Collateral"
    assert first.url == "http://www.boj.or.jp/en/mopo/mpmdeci/mpr_2026/mpr260925a.pdf"


def test_normalize_boj_handles_empty_description_tag(boj_adapter, boj_response):
    # BOJ-фід має <description></description> (тег є, завжди порожній,
    # на відміну від ECB, де тега взагалі нема) -- обидва варіанти
    # мають нормалізуватись до description=None у raw_payload.
    records = boj_adapter.normalize(boj_response)
    assert records[0].raw_payload["description"] is None


def test_normalize_boj_parses_jst_offset_pubdate(boj_adapter, boj_response):
    # +0900 (JST) -- третій відмінний часовий зсув після Fed (GMT) і
    # ECB (+0200), той самий парсер має впоратись без змін коду.
    records = boj_adapter.normalize(boj_response)
    assert records[0].published_at == datetime(
        2026, 9, 25, 17, 0, 0, tzinfo=timezone(timedelta(hours=9))
    )


def test_normalize_sets_fetched_at_close_to_now(fed_adapter, fed_response):
    records = fed_adapter.normalize(fed_response)
    now = datetime.now(timezone.utc)
    for record in records:
        assert (now - record.fetched_at).total_seconds() < 5


def test_normalize_handles_malformed_xml_without_raising(fed_adapter):
    records = fed_adapter.normalize("<rss><channel><item><title>broken")
    assert records == []


def test_normalize_handles_empty_channel(fed_adapter):
    records = fed_adapter.normalize("<rss><channel></channel></rss>")
    assert records == []


def test_normalize_handles_utf8_bom_bytes(fed_adapter, fed_response):
    # Regression: живий запит до Fed (2026-09-26) повернув UTF-8 BOM
    # без charset у HTTP-заголовку -- .text помилково вгадував
    # ISO-8859-1 і ламав XML. fetch() тепер повертає response.content
    # (байти) саме з цієї причини -- цей тест підтверджує, що
    # normalize() коректно парсить байти з BOM (не .text/str).
    raw_bytes_with_bom = b"\xef\xbb\xbf" + fed_response.encode("utf-8")
    records = fed_adapter.normalize(raw_bytes_with_bom)
    assert len(records) == 2
    assert records[0].title == "Federal Reserve Board announces approval of application by Peoples Bancorp Inc."


# ============================================================
# Формати дати публікації
# ============================================================
# Живий прогін 2026-10-05 (docs/decisions.md): адаптер знав ЛИШЕ
# RFC 822, тож `yahoo_finance_rss` втрачав ВСІ 49 статей, а
# `investing_rss` — усі 10, і в лозі це виглядало як "отримано 0
# записів" при HTTP 200 — тихий збій того самого класу, що й мʼякий
# троттл GDELT.


@pytest.mark.parametrize(
    "raw,expected_iso",
    [
        # RFC 822 — за специфікацією RSS 2.0 (Fed/ECB/BOJ/BBC/CNBC).
        ("Mon, 05 Oct 2026 16:18:38 GMT", "2026-10-05T16:18:38+00:00"),
        # ISO 8601 з "Z" — Yahoo Finance.
        ("2026-09-23T01:52:26Z", "2026-09-23T01:52:26+00:00"),
        # ISO 8601 зі зсувом — NPR.
        ("2026-10-04T08:29:38-04:00", "2026-10-04T08:29:38-04:00"),
    ],
)
def test_parse_pub_date_known_live_formats(raw, expected_iso):
    assert parse_pub_date(raw).isoformat() == expected_iso


def test_parse_pub_date_space_separated_without_zone():
    """Investing.com: "2026-10-05 16:18:38" — без T і без зони.
    Зона не вгадується, викликач доповнює UTC."""
    parsed = parse_pub_date("2026-10-05 16:18:38")
    assert parsed is not None
    assert parsed.tzinfo is None


def test_parse_pub_date_returns_none_on_garbage():
    assert parse_pub_date("не дата") is None
    assert parse_pub_date("") is None
