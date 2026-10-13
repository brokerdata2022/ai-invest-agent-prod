"""
Тести розбору виступів членів ЦБ — на РЕАЛЬНИХ назвах подій із фіду
(знято живим запитом 2026-10-13: 31 виступ зі 110 подій).
"""

import pytest

from speeches import detect_institution, extract_speaker, is_speech


@pytest.mark.parametrize("title", [
    "FOMC Member Waller Speaks",
    "ECB President Lagarde Speaks",
    "MPC Member Breeden Speaks",
    "Fed Chairman Warsh Speaks",
    "BOE Gov Bailey Testifies",
])
def test_real_feed_titles_recognised_as_speeches(title):
    assert is_speech(title)


@pytest.mark.parametrize("title", [
    "CPI y/y", "Core Retail Sales m/m", "Unemployment Claims",
    "ECB Press Conference", "Crude Oil Inventories",
])
def test_data_releases_are_not_speeches(title):
    """Реліз даних не є виступом: у нього є значення, очікування й
    факт для порівняння, тобто він належить `release_log`, не
    `policy_speeches`."""
    assert not is_speech(title)


@pytest.mark.parametrize("title, expected", [
    ("FOMC Member Waller Speaks", "fed"),
    ("Fed Chairman Warsh Speaks", "fed"),
    ("ECB President Lagarde Speaks", "ecb"),
    ("MPC Member Mann Speaks", "boe"),
    ("BOJ Governor Ueda Speaks", "boj"),
])
def test_institution_detected(title, expected):
    """Інституція критична: синтез ставки ФРС не має спиратись на
    виступ члена Банку Англії, навіть якщо той звучав переконливо."""
    assert detect_institution(title) == expected


def test_unknown_institution_is_none_not_guess():
    assert detect_institution("Some Official Speaks") is None


@pytest.mark.parametrize("title, expected", [
    ("FOMC Member Waller Speaks", "Waller"),
    ("ECB President Lagarde Speaks", "Lagarde"),
    ("MPC Member Breeden Speaks", "Breeden"),
])
def test_speaker_extracted_without_titles(title, expected):
    assert extract_speaker(title) == expected
