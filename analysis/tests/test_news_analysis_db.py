"""
Регресія 2026-09-29 (живий баг): save_synthesis()/save_market_synthesis()
— UPSERT "один рядок на день" (ON CONFLICT ... DO UPDATE), але жоден НЕ
скидав notified_at при оновленні існуючого рядка. (Третім у цій трійці
був save_candidate(); 2026-10-05 він переїхав у discovery/_db.py разом
із переписаним пошуком новачків — його версія того самого тесту тепер
у test_discovery_db.py.)
Наслідок, підтверджений живо: актив уже надіслано вранці нотифай-джобою
→ повторний синтез того самого дня (напр. ручний перезапуск чи новий
цикл цін/новин) оновлює summary/direction/confidence на СПРАВДІ нові, але
`notified_at` лишається зі старого відправлення → notify_*-скрипт
(`WHERE notified_at IS NULL`) більше ніколи не бачить оновлений рядок аж
до наступного календарного дня — свіжий аналіз мовчки губиться.

Фікс: усі ON CONFLICT DO UPDATE SET тепер додають `notified_at = NULL`
поруч із `created_at`/`discovered_at = now()` — оновлений вміст знову стає
"ще не надісланим".

Той самий підхід, що test_consolidated_db.py: фейковий cursor/conn, що
записує виконаний SQL, без реальної БД.
"""

from decimal import Decimal

import pytest

from news_analysis._db import save_market_synthesis, save_synthesis
from news_analysis.aggregate import AssetSignal, NewsCluster
from news_analysis.prices import PriceChange


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params):
        self._conn.executed.append((query, params))

    def fetchone(self):
        return (1,)


class _FakeConn:
    def __init__(self):
        self.executed = []
        self.committed = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed += 1


class _Result:
    def __init__(self, summary="s", direction="up", confidence=0.7, reasoning="r", confirmation_factors="c"):
        self.summary = summary
        self.direction = direction
        self.confidence = confidence
        self.reasoning = reasoning
        self.confirmation_factors = confirmation_factors


def _upsert_set_clause(query: str) -> str:
    """SET-частина ON CONFLICT DO UPDATE — щоб не зачепити INSERT-частину
    того самого запиту, де notified_at свідомо відсутній (нема чого
    скидати на щойно вставленому рядку, default і так NULL)."""
    assert "DO UPDATE SET" in query
    return query.split("DO UPDATE SET", 1)[1]


def test_save_synthesis_upsert_resets_notified_at():
    conn = _FakeConn()
    signal = AssetSignal(asset_id="xauusd", cluster_count=3, direction_counts={}, net_lean=2, summaries=["x"])
    price = PriceChange(
        asset_id="xauusd", start_value=Decimal("100"), end_value=Decimal("97"),
        start_date="2026-09-28", end_date="2026-09-29", pct_change=Decimal("-3.0"),
    )

    save_synthesis(conn, "xauusd", signal, price, window_days=3, result=_Result(), llm_call_id=1)

    assert conn.committed == 1
    query, _ = conn.executed[0]
    assert "notified_at = NULL" in _upsert_set_clause(query)


def test_save_synthesis_persists_confirmation_factors():
    # 2026-10-02: confirmation_factors -- нове поле LLM-висновку
    # (synthesize.py:PriceNewsSynthesisResult), має й записуватись у
    # INSERT-частину, і скидатись у DO UPDATE SET, як і summary.
    conn = _FakeConn()
    signal = AssetSignal(asset_id="xauusd", cluster_count=3, direction_counts={}, net_lean=2, summaries=["x"])
    price = PriceChange(
        asset_id="xauusd", start_value=Decimal("100"), end_value=Decimal("97"),
        start_date="2026-09-28", end_date="2026-09-29", pct_change=Decimal("-3.0"),
    )
    result = _Result(confirmation_factors="Якщо падіння продовжиться без новин -- це вже не корекція.")

    save_synthesis(conn, "xauusd", signal, price, window_days=3, result=result, llm_call_id=1)

    query, params = conn.executed[0]
    assert "confirmation_factors = EXCLUDED.confirmation_factors" in _upsert_set_clause(query)
    assert result.confirmation_factors in params


def test_save_market_synthesis_upsert_resets_notified_at():
    conn = _FakeConn()
    cluster = NewsCluster(
        representative_title="t", source_count=2, asset_ids=[], dominant_direction="up",
        direction_counts={}, summaries=["s"], urls=[], earliest_published_at=None,
        latest_published_at=None,
    )

    save_market_synthesis(conn, clusters=[cluster], macro={}, window_days=7, result=_Result(), llm_call_id=1)

    assert conn.committed == 1
    query, _ = conn.executed[0]
    assert "notified_at = NULL" in _upsert_set_clause(query)


@pytest.mark.parametrize(
    "save_call",
    [
        lambda conn: save_synthesis(
            conn, "xauusd",
            AssetSignal(asset_id="xauusd", cluster_count=1, direction_counts={}, net_lean=1, summaries=[]),
            PriceChange(asset_id="xauusd", start_value=Decimal("1"), end_value=Decimal("1"),
                        start_date="2026-09-28", end_date="2026-09-29", pct_change=Decimal("0")),
            3, _Result(), 1,
        ),
        lambda conn: save_market_synthesis(conn, clusters=[], macro={}, window_days=7, result=_Result(), llm_call_id=1),
    ],
    ids=["save_synthesis", "save_market_synthesis"],
)
def test_upsert_still_commits_and_returns_id(save_call):
    """Регресія самого фіксу: доводить, що додавання notified_at = NULL
    не зламало решту UPSERT (commit і RETURNING id так само працюють)."""
    conn = _FakeConn()
    result_id = save_call(conn)
    assert result_id == 1
    assert conn.committed == 1
