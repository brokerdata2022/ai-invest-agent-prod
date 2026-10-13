"""
Тест реєстру RSS_FEEDS — чиста перевірка конфігурації, без мережі
(сам RssAdapter уже тестується на фікстурах у test_rss_adapter.py).
"""

from collections import Counter

from common.news_adapter import STREAMS
from news.rss_feeds import RSS_FEEDS

_GEOPOLITICAL_FEEDS = {
    "fed_rss", "ecb_rss", "boj_rss",
    # Промови й свідчення Fed додані 2026-10-13 для повного синтезу
    # ставки: `fed_rss` це `press_all.xml`, тобто прес-релізи БЕЗ
    # промов, а позиція комітету формується в публічних виступах
    # задовго до засідання (постановка користувача 2026-10-11:
    # "+ виступи банків та голів комісій").
    "fed_speeches_rss", "fed_testimony_rss",
    "bbc_rss", "aljazeera_rss", "guardian_rss", "npr_rss", "skynews_rss", "dw_rss",
}

# Потік `general` (ринок/бізнес) — доданий 2026-10-05, docs/decisions.md.
_GENERAL_FEEDS = {
    "cnbc_markets_rss", "marketwatch_rss", "investing_rss",
    "bbc_business_rss", "guardian_business_rss", "npr_business_rss", "ft_rss",
}


def test_all_expected_feeds_registered():
    assert set(RSS_FEEDS) == _GEOPOLITICAL_FEEDS | _GENERAL_FEEDS


def test_every_feed_has_https_url():
    for name, cfg in RSS_FEEDS.items():
        assert cfg["url"].startswith("https://"), f"{name}: url має бути https"


def test_no_duplicate_feed_urls():
    """Два імені на той самий URL дали б фальшиве відчуття двох
    незалежних джерел — а `aggregate.py:cluster_articles()` рахує
    значущість новини саме за кількістю НЕЗАЛЕЖНИХ джерел."""
    duplicates = [url for url, count in Counter(c["url"] for c in RSS_FEEDS.values()).items() if count > 1]
    assert not duplicates, f"дублі URL: {duplicates}"


def test_feed_streams_are_known():
    for name, cfg in RSS_FEEDS.items():
        assert cfg["stream"] in STREAMS, f"{name}: невідомий stream {cfg['stream']!r}"


def test_general_stream_has_rss_backup():
    """Регресія на живий збій 2026-10-05: потік `general` мав ЄДИНЕ
    джерело — GDELT, і коли IP потрапив у штрафний стан, потік помер
    (нуль статей за 7 днів). Фідбек користувача: "а чого до цієї пори
    не має резервних джерел". RSS не має лімітів запитів, тож це
    резерв, що не залежить від стану GDELT."""
    general = {name for name, cfg in RSS_FEEDS.items() if cfg["stream"] == "general"}
    assert general == _GENERAL_FEEDS
    # Кілька НЕЗАЛЕЖНИХ видань, не одне: значущість новини
    # визначається збігом у кількох джерелах (той самий принцип, що
    # для geopolitical).
    assert len(general) >= 5


def test_geopolitical_stream_unchanged():
    geopolitical = {name for name, cfg in RSS_FEEDS.items() if cfg["stream"] == "geopolitical"}
    assert geopolitical == _GEOPOLITICAL_FEEDS
