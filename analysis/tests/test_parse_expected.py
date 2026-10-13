from expectations.parse_expected import parse_expected_value


def test_parses_plain_percent():
    assert parse_expected_value("0.6%") == 0.6


def test_parses_negative_percent():
    assert parse_expected_value("-0.3%") == -0.3


def test_parses_thousands_suffix():
    assert parse_expected_value("615K") == 615_000


def test_parses_millions_suffix():
    assert parse_expected_value("1.35M") == 1_350_000


def test_parses_negative_billions_suffix():
    assert parse_expected_value("-258B") == -258_000_000_000


def test_parses_plain_number_without_suffix_or_percent():
    assert parse_expected_value("4.1") == 4.1


def test_returns_none_for_empty_string():
    assert parse_expected_value("") is None


def test_returns_none_for_none():
    assert parse_expected_value(None) is None


def test_returns_none_for_unparseable_text():
    assert parse_expected_value("n/a") is None


def test_handles_surrounding_whitespace():
    assert parse_expected_value(" 0.6% ") == 0.6
