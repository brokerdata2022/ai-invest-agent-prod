"""
Повний розбір активу через /ask (2026-10-24): «повний аналіз золота»
дає розгорнутий розбір за чинниками, а не 8 речень переказу.
"""

from datetime import datetime, timezone

import pytest

from assistant import full_analysis


@pytest.mark.parametrize("question", [
    "Дай мені повний аналіз на золото",
    "розбір по BX",
    "розбери нафту",
    "що кажуть чинники по EUR/USD",
    "фундаментал по SKYD",
])
def test_full_analysis_intent(question):
    assert full_analysis.wants_full_analysis(question)


@pytest.mark.parametrize("question", ["чому золото впало?", "що буде з нафтою завтра"])
def test_ordinary_questions_stay_ordinary(question):
    assert not full_analysis.wants_full_analysis(question)


ROW = {
    "id": 1, "asset_id": "xauusd", "asset_kind": "watchlist", "company_name": "Золото",
    "direction": "down", "confidence": 0.6, "summary": "Тиск ставок.", "reasoning": "по чинниках",
    "strengths": [], "risks": [], "created_at": datetime(2026, 10, 24, 11, 26, tzinfo=timezone.utc),
}


def _patch(monkeypatch, calls, latest):
    monkeypatch.setattr(full_analysis.assistant_db, "asset_terms", lambda conn: {"xauusd": (r"золот",), "skyd": (r"skyd",)})
    monkeypatch.setattr(full_analysis.assistant_db, "known_tickers", lambda conn: {"BX", "SKYD"})
    monkeypatch.setattr(full_analysis, "fetch_watchlist_assets", lambda conn: [
        {"asset_id": "xauusd", "ticker": None}, {"asset_id": "skyd", "ticker": "SKYD"},
    ])
    monkeypatch.setattr(full_analysis, "analyze_watchlist_asset",
                        lambda conn, asset, watchlist: calls.append(asset["asset_id"]) or "unchanged")
    monkeypatch.setattr(full_analysis, "analyze_stock", lambda conn, stock: calls.append(stock["ticker"]) or "analyzed")
    monkeypatch.setattr(full_analysis, "fetch_stock_for_analysis", lambda conn, t: {"ticker": t})
    monkeypatch.setattr(full_analysis, "fetch_latest_full_analysis", lambda conn, asset_id: latest.get(asset_id))


def test_reply_refreshes_through_gate_and_shows_date(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, {"xauusd": ROW})
    text = full_analysis.full_analysis_reply(None, "повний аналіз золото", lambda row: f"РОЗБІР {row['asset_id']}")
    assert calls == ["xauusd"]
    assert text.startswith("РОЗБІР xauusd")
    assert "Висновок від 24.10 14:26" in text


def test_watchlist_stock_is_not_analyzed_twice_as_ticker(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, {"skyd": dict(ROW, asset_id="skyd"), "BX": dict(ROW, asset_id="BX")})
    full_analysis.full_analysis_reply(None, "розбір skyd SKYD і BX", lambda row: row["asset_id"])
    assert calls == ["skyd", "BX"]


def test_no_intent_or_no_asset_falls_back_to_ordinary_answer(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, {"xauusd": ROW})
    assert full_analysis.full_analysis_reply(None, "чому золото впало?", str) is None
    assert full_analysis.full_analysis_reply(None, "повний аналіз ринку", str) is None
    assert calls == []


def test_failed_refresh_still_shows_last_analysis(monkeypatch):
    calls = []
    _patch(monkeypatch, calls, {"xauusd": ROW})

    def boom(conn, asset, watchlist):
        raise RuntimeError("DeepSeek лежить")

    class _Conn:
        def rollback(self):
            pass

    monkeypatch.setattr(full_analysis, "analyze_watchlist_asset", boom)
    text = full_analysis.full_analysis_reply(_Conn(), "аналіз золото", lambda row: "РОЗБІР")
    assert text.startswith("РОЗБІР")
