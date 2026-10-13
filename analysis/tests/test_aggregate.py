"""
Тести кластеризації/агрегації — чисті функції, без БД/мережі.
Приклад дублікатів взято з реального живого прогону 2026-09-26
(docs/decisions.md) — та сама AP wire-стрічка "Asian shares mixed
after global bond sell-off" прийшла з кількох різних видань.
"""

from datetime import datetime, timezone

from news_analysis.aggregate import (
    aggregate_by_asset,
    cluster_articles,
    normalize_title,
    top_clusters,
)


def _article(title, url, published_at, asset_id=None, direction="unclear", summary="", raw_news_id=None):
    return {
        "title": title,
        "url": url,
        "published_at": published_at,
        "asset_id": asset_id,
        "direction": direction,
        "summary": summary,
        "raw_news_id": raw_news_id,
    }


_WIRE_TITLE = "Asian shares are mixed after global bond sell-off and drop in oil prices"

# Реальні дублікати з живого прогону — той самий wire-текст, різні сайти.
_WIRE_DUPLICATES = [
    _article(_WIRE_TITLE, "https://www.wral.com/a", datetime(2026, 9, 25, 10, tzinfo=timezone.utc),
              "wti_crude", "down", "Oil dropped alongside a global bond selloff."),
    _article(_WIRE_TITLE, "https://www.clickorlando.com/a", datetime(2026, 9, 25, 9, tzinfo=timezone.utc),
              "wti_crude", "down", "Oil dropped alongside a global bond selloff."),
    _article(_WIRE_TITLE, "https://www.stcatharinesstandard.ca/a", datetime(2026, 9, 25, 11, tzinfo=timezone.utc),
              None, "unclear", "Asian markets mixed amid bond selloff."),
    _article(_WIRE_TITLE + ".", "https://www.therecord.com/a", datetime(2026, 9, 25, 8, tzinfo=timezone.utc),
              "wti_crude", "down", "Oil dropped alongside a global bond selloff."),
]

_UNRELATED = _article(
    "Gold prices rise on festive season demand",
    "https://example.com/gold",
    datetime(2026, 9, 25, 12, tzinfo=timezone.utc),
    "xauusd", "up", "Retail demand for gold jumped ahead of the festive season.",
)


def test_normalize_title_strips_punctuation_and_case():
    assert normalize_title("Asian shares are mixed!") == "asian shares are mixed"


def test_normalize_title_preserves_non_latin_text():
    # Regression: a-z0-9-only regex перетворювало нелатинський текст
    # на майже порожній рядок (докладніше docs/decisions.md,
    # 2026-09-26) -- \w має зберігати гінді/китайську/грецьку.
    normalized = normalize_title("कच्चे तेल में फिर गिरावट")
    assert len(normalized) > 10
    assert normalized != ""


def test_cluster_articles_does_not_merge_unrelated_non_latin_stories():
    # Regression: живий прогін 2026-09-26 показав, що заголовок про
    # нафту (гінді) і заголовок про дохідність облігацій Японії
    # (китайська) помилково потрапили в один кластер, бо старий
    # regex (a-z0-9 only) залишав від обох майже порожні рядки, які
    # SequenceMatcher вважав "схожими". Це РІЗНІ історії -- мають
    # лишитись в окремих кластерах.
    oil_hindi = _article(
        "कच्चे तेल में फिर गिरावट, ब्रेंट 106 डॉलर के नीचे",
        "https://example.com/oil-hindi",
        datetime(2026, 9, 25, tzinfo=timezone.utc),
        "brent_crude", "down", "Brent crude fell below $106.",
    )
    japan_bonds_chinese = _article(
        "日本10年期国债收益率创下历史新高",
        "https://example.com/japan-bonds",
        datetime(2026, 9, 25, tzinfo=timezone.utc),
        None, "up", "Japanese 10-year bond yields hit a record high.",
    )

    clusters = cluster_articles([oil_hindi, japan_bonds_chinese])

    assert len(clusters) == 2
    assert {c.source_count for c in clusters} == {1, 1}


def test_cluster_articles_groups_wire_duplicates():
    clusters = cluster_articles(_WIRE_DUPLICATES)
    assert len(clusters) == 1
    assert clusters[0].source_count == 4


def test_cluster_articles_keeps_unrelated_stories_separate():
    clusters = cluster_articles(_WIRE_DUPLICATES + [_UNRELATED])
    assert len(clusters) == 2
    sizes = sorted(c.source_count for c in clusters)
    assert sizes == [1, 4]


def test_cluster_articles_sorted_by_latest_published_desc():
    clusters = cluster_articles(_WIRE_DUPLICATES + [_UNRELATED])
    assert clusters[0].latest_published_at >= clusters[1].latest_published_at


def test_cluster_union_of_asset_ids_across_group():
    clusters = cluster_articles(_WIRE_DUPLICATES)
    assert clusters[0].asset_ids == ["wti_crude"]  # None ігнорується, дублікати не повторюються


def test_cluster_dominant_direction_prefers_decisive_over_unclear():
    # 3 "down", 1 "unclear" в тій самій групі -- домінує "down".
    clusters = cluster_articles(_WIRE_DUPLICATES)
    assert clusters[0].dominant_direction == "down"


def test_cluster_representative_title_is_longest():
    clusters = cluster_articles(_WIRE_DUPLICATES)
    assert clusters[0].representative_title == _WIRE_TITLE + "."


def test_cluster_summaries_deduplicated():
    clusters = cluster_articles(_WIRE_DUPLICATES)
    # 3 статті мають той самий summary -- має лишитись один раз.
    assert clusters[0].summaries.count("Oil dropped alongside a global bond selloff.") == 1


def test_aggregate_by_asset_counts_clusters_not_raw_articles():
    clusters = cluster_articles(_WIRE_DUPLICATES + [_UNRELATED])
    signals = aggregate_by_asset(clusters)

    # wti_crude -- 1 кластер (4 статті об'єднані), не 3 (стільки, скільки
    # статей мали asset_id=wti_crude до кластеризації).
    assert signals["wti_crude"].cluster_count == 1
    assert signals["wti_crude"].direction_counts["down"] == 1
    assert signals["xauusd"].cluster_count == 1
    assert signals["xauusd"].direction_counts["up"] == 1


def test_aggregate_by_asset_net_lean():
    clusters = cluster_articles([_UNRELATED])
    signals = aggregate_by_asset(clusters)
    assert signals["xauusd"].net_lean == 1  # 1 up, 0 down


def test_cluster_articles_empty_input():
    assert cluster_articles([]) == []


def test_aggregate_by_asset_empty_input():
    assert aggregate_by_asset([]) == {}


def test_top_clusters_sorted_by_source_count_desc():
    clusters = cluster_articles(_WIRE_DUPLICATES + [_UNRELATED])
    top = top_clusters(clusters, limit=8)
    assert [c.source_count for c in top] == [4, 1]


def test_top_clusters_respects_limit():
    clusters = cluster_articles(_WIRE_DUPLICATES + [_UNRELATED])
    top = top_clusters(clusters, limit=1)
    assert len(top) == 1
    assert top[0].source_count == 4


def test_top_clusters_empty_input():
    assert top_clusters([]) == []


def test_aggregate_by_asset_skips_articles_without_asset_id():
    only_unclear = [
        _article("Some geopolitical event happens", "https://example.com/x",
                 datetime(2026, 9, 25, tzinfo=timezone.utc), None, "unclear", "No asset tied to this.")
    ]
    clusters = cluster_articles(only_unclear)
    signals = aggregate_by_asset(clusters)
    assert signals == {}


def test_cluster_articles_tolerates_raw_pre_analysis_dicts():
    """Регресія 2026-09-28: consolidate.py тепер кластеризує СИРІ
    статті (ще без direction/asset_id/summary — ці поля з'являються
    лише після LLM-аналізу). Раніше _build_cluster падав з KeyError на
    відсутньому "direction"."""
    raw_articles = [
        {"title": "Bitcoin tops $84000", "url": "https://a.com/1",
         "published_at": datetime(2026, 9, 27, tzinfo=timezone.utc), "raw_news_id": 1},
        {"title": "Bitcoin tops $84,000", "url": "https://b.com/1",
         "published_at": datetime(2026, 9, 27, tzinfo=timezone.utc), "raw_news_id": 2},
    ]
    clusters = cluster_articles(raw_articles)
    assert len(clusters) == 1
    assert clusters[0].source_count == 2
    assert clusters[0].dominant_direction == "unclear"
    assert clusters[0].asset_ids == []
