from decimal import Decimal

from screening.tier_c import ttm_sum, passes_tier_c, MIN_PE, MAX_PE, MAX_PS, MAX_PEG


# --- ttm_sum ---

def test_ttm_sum_basic():
    series = [
        ("2024-12-31", Decimal("1.0")),
        ("2024-09-30", Decimal("1.0")),
        ("2024-06-30", Decimal("1.0")),
        ("2024-03-31", Decimal("1.0")),
        ("2023-12-31", Decimal("1.0")),
    ]
    assert ttm_sum(series) == Decimal("4.0")


def test_ttm_sum_insufficient_history():
    series = [
        ("2024-12-31", Decimal("1.0")),
        ("2024-09-30", Decimal("1.0")),
    ]
    assert ttm_sum(series) is None


def test_ttm_sum_exact_n():
    series = [
        ("2024-12-31", Decimal("2.5")),
        ("2024-09-30", Decimal("2.0")),
        ("2024-06-30", Decimal("1.5")),
        ("2024-03-31", Decimal("1.0")),
    ]
    assert ttm_sum(series) == Decimal("7.0")


def test_ttm_sum_custom_n():
    series = [
        ("2024-12-31", Decimal("1.0")),
        ("2024-09-30", Decimal("1.0")),
    ]
    assert ttm_sum(series, n=2) == Decimal("2.0")


# --- passes_tier_c: успішний випадок ---

def test_passes_tier_c_all_pass():
    # price=100, eps_ttm=5 -> pe=20 (в межах 10-35)
    # market_cap=1000, revenue_ttm=200 -> ps=5 (< 8)
    # eps_growth_yoy=0.20 -> peg = 20 / (0.20*100) = 1.0 (< 2)
    passed, details = passes_tier_c(
        price=Decimal("100"),
        market_cap=Decimal("1000"),
        eps_ttm=Decimal("5"),
        revenue_ttm=Decimal("200"),
        eps_growth_yoy=Decimal("0.20"),
    )
    assert passed is True
    assert details["pe"] == Decimal("20")
    assert details["ps"] == Decimal("5")
    assert details["peg"] == Decimal("1.0")
    assert "fail_reason" not in details


# --- eps_ttm ---

def test_passes_tier_c_eps_ttm_none():
    passed, details = passes_tier_c(
        price=Decimal("100"),
        market_cap=Decimal("1000"),
        eps_ttm=None,
        revenue_ttm=Decimal("200"),
        eps_growth_yoy=Decimal("0.20"),
    )
    assert passed is False
    assert details["fail_reason"] == "eps_ttm_missing_or_negative"


def test_passes_tier_c_eps_ttm_negative():
    passed, details = passes_tier_c(
        price=Decimal("100"),
        market_cap=Decimal("1000"),
        eps_ttm=Decimal("-1"),
        revenue_ttm=Decimal("200"),
        eps_growth_yoy=Decimal("0.20"),
    )
    assert passed is False
    assert details["fail_reason"] == "eps_ttm_missing_or_negative"


# --- P/E межі ---

def test_passes_tier_c_pe_too_low():
    # price=100, eps_ttm=20 -> pe=5 (< MIN_PE=10)
    passed, details = passes_tier_c(
        price=Decimal("100"),
        market_cap=Decimal("1000"),
        eps_ttm=Decimal("20"),
        revenue_ttm=Decimal("200"),
        eps_growth_yoy=Decimal("0.20"),
    )
    assert passed is False
    assert details["fail_reason"] == "pe_out_of_range"


def test_passes_tier_c_pe_too_high():
    # price=100, eps_ttm=2 -> pe=50 (> MAX_PE=35)
    passed, details = passes_tier_c(
        price=Decimal("100"),
        market_cap=Decimal("1000"),
        eps_ttm=Decimal("2"),
        revenue_ttm=Decimal("200"),
        eps_growth_yoy=Decimal("0.20"),
    )
    assert passed is False
    assert details["fail_reason"] == "pe_out_of_range"


def test_passes_tier_c_pe_boundary_min_excluded():
    # pe строго = MIN_PE -> не проходить (виключно >)
    passed, details = passes_tier_c(
        price=Decimal("100"),
        market_cap=Decimal("1000"),
        eps_ttm=Decimal("10"),  # pe = 10 = MIN_PE
        revenue_ttm=Decimal("200"),
        eps_growth_yoy=Decimal("0.20"),
    )
    assert passed is False
    assert details["fail_reason"] == "pe_out_of_range"


def test_passes_tier_c_pe_boundary_max_excluded():
    # pe строго = MAX_PE -> не проходить
    passed, details = passes_tier_c(
        price=Decimal("350"),
        market_cap=Decimal("1000"),
        eps_ttm=Decimal("10"),  # pe = 35 = MAX_PE
        revenue_ttm=Decimal("200"),
        eps_growth_yoy=Decimal("0.20"),
    )
    assert passed is False
    assert details["fail_reason"] == "pe_out_of_range"


# --- P/S ---

def test_passes_tier_c_revenue_ttm_none():
    passed, details = passes_tier_c(
        price=Decimal("100"),
        market_cap=Decimal("1000"),
        eps_ttm=Decimal("5"),
        revenue_ttm=None,
        eps_growth_yoy=Decimal("0.20"),
    )
    assert passed is False
    assert details["fail_reason"] == "revenue_ttm_missing_or_negative"
    assert "pe" in details  # pe вже обчислений до цієї точки


def test_passes_tier_c_ps_too_high():
    # market_cap=1000, revenue_ttm=100 -> ps=10 (>= MAX_PS=8)
    passed, details = passes_tier_c(
        price=Decimal("100"),
        market_cap=Decimal("1000"),
        eps_ttm=Decimal("5"),
        revenue_ttm=Decimal("100"),
        eps_growth_yoy=Decimal("0.20"),
    )
    assert passed is False
    assert details["fail_reason"] == "ps_too_high"


def test_passes_tier_c_ps_boundary_excluded():
    # ps строго = MAX_PS -> не проходить (виключно <)
    passed, details = passes_tier_c(
        price=Decimal("100"),
        market_cap=Decimal("800"),
        eps_ttm=Decimal("5"),
        revenue_ttm=Decimal("100"),  # ps = 8 = MAX_PS
        eps_growth_yoy=Decimal("0.20"),
    )
    assert passed is False
    assert details["fail_reason"] == "ps_too_high"


# --- PEG ---

def test_passes_tier_c_eps_growth_none():
    passed, details = passes_tier_c(
        price=Decimal("100"),
        market_cap=Decimal("1000"),
        eps_ttm=Decimal("5"),
        revenue_ttm=Decimal("200"),
        eps_growth_yoy=None,
    )
    assert passed is False
    assert details["fail_reason"] == "eps_growth_missing_or_negative"
    assert "ps" in details  # ps вже обчислений до цієї точки


def test_passes_tier_c_eps_growth_negative():
    passed, details = passes_tier_c(
        price=Decimal("100"),
        market_cap=Decimal("1000"),
        eps_ttm=Decimal("5"),
        revenue_ttm=Decimal("200"),
        eps_growth_yoy=Decimal("-0.05"),
    )
    assert passed is False
    assert details["fail_reason"] == "eps_growth_missing_or_negative"


def test_passes_tier_c_peg_too_high():
    # pe=20, eps_growth_yoy=0.05 -> peg = 20 / 5 = 4.0 (>= MAX_PEG=2)
    passed, details = passes_tier_c(
        price=Decimal("100"),
        market_cap=Decimal("1000"),
        eps_ttm=Decimal("5"),
        revenue_ttm=Decimal("200"),
        eps_growth_yoy=Decimal("0.05"),
    )
    assert passed is False
    assert details["fail_reason"] == "peg_too_high"


def test_passes_tier_c_peg_boundary_excluded():
    # pe=20, eps_growth_yoy=0.10 -> peg = 20 / 10 = 2.0 = MAX_PEG -> не проходить
    passed, details = passes_tier_c(
        price=Decimal("100"),
        market_cap=Decimal("1000"),
        eps_ttm=Decimal("5"),
        revenue_ttm=Decimal("200"),
        eps_growth_yoy=Decimal("0.10"),
    )
    assert passed is False
    assert details["fail_reason"] == "peg_too_high"


def test_criteria_constants():
    # Захист від випадкової зміни порогів без оновлення документації
    assert MIN_PE == Decimal("10")
    assert MAX_PE == Decimal("35")
    assert MAX_PS == Decimal("8")
    assert MAX_PEG == Decimal("2")
