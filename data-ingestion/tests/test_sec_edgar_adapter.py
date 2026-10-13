"""
Тести SecEdgarAdapter на фікстурах, побудованих за задокументованою
(кількома незалежними джерелами підтвердженою) формою відповіді
SEC EDGAR — жива перевірка з цієї сесії недоступна (sec.gov немає в
дозволених доменах пісочниці), тому фікстури це не "справжній" live
дамп, а найкраще наближення. Перший реальний прогін користувача —
фактична перевірка.

raw_response, який normalize() отримує від fetch(), має форму
{suffix: [payload, ...]} — список, бо METRICS тепер дозволяє кілька
тегів-варіантів на один концепт (revenue: Revenues +
RevenueFromContractWithCustomerExcludingAssessedTax).
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from companies.sec_edgar_adapter import METRICS, SecEdgarAdapter

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(name):
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def adapter():
    return SecEdgarAdapter(api_key="test-agent contact@example.com", ticker="aapl")


def test_ticker_normalized_to_uppercase(adapter):
    assert adapter.ticker == "AAPL"
    assert adapter.source == "sec_edgar"


def test_missing_user_agent_rejected():
    with pytest.raises(ValueError):
        SecEdgarAdapter(api_key="", ticker="AAPL")


def test_invalid_ticker_rejected():
    with pytest.raises(ValueError):
        SecEdgarAdapter(api_key="test-agent", ticker="")


def test_explicit_cik_skips_resolution():
    adapter = SecEdgarAdapter(api_key="test-agent", ticker="AAPL", cik="320193")
    assert adapter.cik == "0000320193"


def test_resolve_cik_from_tickers_json(adapter, monkeypatch):
    tickers_payload = _load("sec_edgar_tickers_response.json")

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return tickers_payload

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            assert "User-Agent" in headers
            return FakeResponse()

    adapter.session = FakeSession()
    cik = adapter._resolve_cik()
    assert cik == "0000320193"


def test_resolve_cik_unknown_ticker_raises(monkeypatch):
    adapter = SecEdgarAdapter(api_key="test-agent", ticker="NOPE")
    tickers_payload = _load("sec_edgar_tickers_response.json")

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return tickers_payload

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return FakeResponse()

    adapter.session = FakeSession()
    with pytest.raises(ValueError):
        adapter._resolve_cik()


def test_normalize_duration_concept_filters_cumulative_and_wrong_forms(adapter):
    raw = {"revenue": [_load("sec_edgar_revenues_response.json")]}
    records = adapter.normalize(raw)

    # 4 записи у фікстурі: 2 квартальні (10-K, ~91-92 дні), 1 кумулятивний
    # (9 місяців — відкинуто), 1 8-K (відкинуто за формою) => лишається 2
    assert len(records) == 2
    assert all(r.metric_id == "aapl_revenue" for r in records)

    values = {r.observed_at: r.value for r in records}
    assert values[date(2025, 9, 30)] == Decimal("100000000000")
    assert values[date(2024, 9, 30)] == Decimal("95000000000")
    # кумулятивний запис (9 місяців, той самий end що і квартальний) не потрапив
    assert Decimal("280000000000") not in values.values()


def test_normalize_instant_concept_no_start_kept(adapter):
    raw = {"shares_outstanding": [_load("sec_edgar_shares_response.json")]}
    records = adapter.normalize(raw)

    assert len(records) == 1
    assert records[0].metric_id == "aapl_shares_outstanding"
    assert records[0].value == Decimal("15000000000")
    assert records[0].observed_at == date(2025, 9, 30)
    assert records[0].revision is None  # revision визначає common/db.py, не адаптер


def test_normalize_deduplicates_split_adjusted_duplicate_by_latest_filed(adapter):
    # Реальний кейс з живого тесту 2026-09-21: та сама дата (2012-12-29),
    # два записи з різним val через перерахунок EPS після спліту акцій.
    raw = {"eps_diluted": [_load("sec_edgar_eps_split_duplicate_response.json")]}
    records = adapter.normalize(raw)

    assert len(records) == 1  # не 2 — дублікат на ту саму дату злито в один
    assert records[0].metric_id == "aapl_eps_diluted"
    assert records[0].observed_at == date(2012, 12, 29)
    # береться запис з пізнішим "filed" (2014-04-23 > 2013-01-23)
    assert records[0].value == Decimal("1.97")


def test_normalize_merges_multiple_tag_variants_for_same_concept(adapter):
    # revenue має 2 варіанти тегу (METRICS) — старий "Revenues" (дані
    # до 2025-09-30) і новий "RevenueFromContract..." (дані з 2026).
    # Обидва мають потрапити в один часовий ряд aapl_revenue.
    raw = {
        "revenue": [
            _load("sec_edgar_revenues_response.json"),          # старий тег
            _load("sec_edgar_revenue_new_tag_response.json"),   # новий тег
        ]
    }
    records = adapter.normalize(raw)
    dates = {r.observed_at for r in records}

    assert date(2025, 9, 30) in dates   # зі старого тега
    assert date(2026, 6, 27) in dates   # з нового тега
    new_tag_record = next(r for r in records if r.observed_at == date(2026, 6, 27))
    assert new_tag_record.value == Decimal("110000000000")
    assert new_tag_record.metric_id == "aapl_revenue"  # той самий metric_id, попри інший тег


def test_normalize_combines_multiple_concepts(adapter):
    raw = {
        "revenue": [_load("sec_edgar_revenues_response.json")],
        "shares_outstanding": [_load("sec_edgar_shares_response.json")],
    }
    records = adapter.normalize(raw)
    metric_ids = {r.metric_id for r in records}
    assert metric_ids == {"aapl_revenue", "aapl_shares_outstanding"}


def test_fetch_skips_404_concepts_gracefully(adapter, monkeypatch):
    monkeypatch.setattr("companies.sec_edgar_adapter.time.sleep", lambda s: None)

    class FakeResponse404:
        status_code = 404

    class FakeResponseOK:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return _load("sec_edgar_shares_response.json")

    call_log = []

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            call_log.append(url)
            if "EntityCommonStockSharesOutstanding" in url:
                return FakeResponseOK()
            return FakeResponse404()

    adapter.cik = "0000320193"  # пропускаємо резолюцію CIK
    adapter.session = FakeSession()
    result = adapter.fetch()

    # тільки shares_outstanding пройшов, решта 404 — пропущені без падіння
    assert set(result.keys()) == {"shares_outstanding"}
    # усі варіанти тегів запитано (не тільки по одному на concept —
    # revenue має 2 варіанти, тому це сума довжин, а не len(METRICS))
    total_variants = sum(len(v) for v in METRICS.values())
    assert len(call_log) == total_variants


def test_fetch_raises_if_all_concepts_missing(adapter, monkeypatch):
    monkeypatch.setattr("companies.sec_edgar_adapter.time.sleep", lambda s: None)

    class FakeResponse404:
        status_code = 404

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return FakeResponse404()

    adapter.cik = "0000320193"
    adapter.session = FakeSession()
    with pytest.raises(ValueError):
        adapter.fetch()
