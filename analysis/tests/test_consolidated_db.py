"""
Тести чистих функцій _consolidated_db.py:is_duplicate_of_recent() —
крос-раннова дедуплікація (2026-09-28, живий баг: та сама новина про
мінфіна Британії з'явилась у news_consolidated ДВІЧІ з двох різних
прогонів consolidate.py, бо LLM-об'єднання бачить лише статті одного
прогону)."""

from news_analysis._consolidated_db import CROSS_RUN_SIMILARITY_THRESHOLD, is_duplicate_of_recent


def test_identical_summary_is_duplicate():
    summary = "Резервний банк Австралії підвищить ставку до 4,6%."
    assert is_duplicate_of_recent(summary, [summary]) is True


def test_paraphrased_summary_is_duplicate():
    old = "Міністр фінансів Великої Британії Хілі пообіцяв 'нову еру індустріалізації' у промові на конференції Лейбористської партії."
    new = "Міністр фінансів Великої Британії Хілі пообіцяв нову еру індустріалізації на конференції Лейбористської партії."
    assert is_duplicate_of_recent(new, [old]) is True


def test_different_summary_is_not_duplicate():
    old = "Резервний банк Австралії підвищить ставку до 4,6%."
    new = "Ціни на золото знижуються під тиском зміцнення долара США."
    assert is_duplicate_of_recent(new, [old]) is False


def test_empty_recent_summaries_never_duplicate():
    assert is_duplicate_of_recent("будь-що", []) is False


def test_threshold_is_reasonably_strict_not_trivial():
    # Занадто низький поріг об'єднав би НЕПОВ'ЯЗАНІ новини того самого
    # активу (напр. дві різні статті про золото).
    assert CROSS_RUN_SIMILARITY_THRESHOLD >= 0.6


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


def test_merge_consolidated_rows_keeps_highest_source_count_as_primary():
    from news_analysis._consolidated_db import merge_consolidated_rows

    rows = [
        {"id": 327, "source_count": 7, "source_raw_news_ids": [1, 2, 3, 4, 5, 6, 7],
         "source_urls": [f"https://a.com/{i}" for i in range(7)]},
        {"id": 361, "source_count": 1, "source_raw_news_ids": [8], "source_urls": ["https://b.com/1"]},
        {"id": 403, "source_count": 1, "source_raw_news_ids": [9], "source_urls": ["https://c.com/1"]},
    ]
    conn = _FakeConn()
    removed = merge_consolidated_rows(conn, rows)

    assert removed == 2
    assert conn.committed is True
    update_query, update_params = conn.cursor_obj.executed[0]
    assert "UPDATE news_consolidated" in update_query
    assert update_params[0] == 9  # 7+1+1 = 9 об'єднаних джерел
    assert update_params[-1] == 327  # primary — найвищий source_count

    delete_query, delete_params = conn.cursor_obj.executed[1]
    assert "DELETE FROM news_consolidated" in delete_query
    assert set(delete_params[0]) == {361, 403}


def test_merge_consolidated_rows_dedupes_overlapping_source_ids():
    from news_analysis._consolidated_db import merge_consolidated_rows

    rows = [
        {"id": 1, "source_count": 2, "source_raw_news_ids": [10, 11], "source_urls": ["https://a.com/1", "https://a.com/2"]},
        {"id": 2, "source_count": 1, "source_raw_news_ids": [11], "source_urls": ["https://a.com/2"]},
    ]
    conn = _FakeConn()
    merge_consolidated_rows(conn, rows)

    _, params = conn.cursor_obj.executed[0]
    assert params[0] == 2  # [10, 11] — дубль 11 не рахується двічі
