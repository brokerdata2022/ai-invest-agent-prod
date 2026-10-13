from decimal import Decimal

from screening.composite_score import percentile_ranks


def test_percentile_ranks_basic_ascending():
    values = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"), Decimal("5")]
    ranks = percentile_ranks(values)
    assert ranks == [Decimal("0"), Decimal("0.25"), Decimal("0.5"), Decimal("0.75"), Decimal("1")]


def test_percentile_ranks_single_value():
    ranks = percentile_ranks([Decimal("42")])
    assert ranks == [Decimal("0.5")]


def test_percentile_ranks_empty():
    assert percentile_ranks([]) == []


def test_percentile_ranks_all_equal():
    # усі однакові -> всі рівні "серединному" рангу
    values = [Decimal("7"), Decimal("7"), Decimal("7")]
    ranks = percentile_ranks(values)
    assert ranks == [Decimal("0.5"), Decimal("0.5"), Decimal("0.5")]


def test_percentile_ranks_with_ties():
    # [1, 2, 2, 3] -- два значення "2" мають однаковий (середній) ранг
    values = [Decimal("1"), Decimal("2"), Decimal("2"), Decimal("3")]
    ranks = percentile_ranks(values)
    # для value=1: less=0, equal=1 -> rank=0 -> pct=0/3=0
    # для value=2 (обидва): less=1, equal=2 -> rank=1+0.5=1.5 -> pct=1.5/3=0.5
    # для value=3: less=3, equal=1 -> rank=3 -> pct=3/3=1
    assert ranks[0] == Decimal("0")
    assert ranks[1] == Decimal("0.5")
    assert ranks[2] == Decimal("0.5")
    assert ranks[3] == Decimal("1")


def test_percentile_ranks_negative_values_for_pe_inversion():
    # імітує -P/E: нижчий P/E має отримати ВИЩИЙ percentile після інверсії
    pe_values = [Decimal("10"), Decimal("20"), Decimal("30")]
    neg_pe = [-v for v in pe_values]
    ranks = percentile_ranks(neg_pe)
    # pe=10 (найдешевший) -> -10 найбільше серед [-10,-20,-30] -> найвищий percentile
    assert ranks[0] == Decimal("1")
    assert ranks[2] == Decimal("0")


def test_percentile_ranks_preserves_order_correspondence():
    # порядок вихідного списку рангів має відповідати порядку вхідних значень
    values = [Decimal("5"), Decimal("1"), Decimal("3")]
    ranks = percentile_ranks(values)
    # value=5 найбільше -> rank=1; value=1 найменше -> rank=0; value=3 середнє -> rank=0.5
    assert ranks[0] == Decimal("1")
    assert ranks[1] == Decimal("0")
    assert ranks[2] == Decimal("0.5")
