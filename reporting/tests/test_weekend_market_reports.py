"""
Повідомлення з урахуванням закритих ринків і нові формати (2026-10-24):
вердикт фундаменталу, watchlist у вихідні, новини, підсумок вихідних.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fundamental_notify import format_verdict_message
from news_notify import drop_closed_markets
from watchlist_notify import format_message, split_by_market
from weekend_wrap_notify import format_message as format_wrap

SATURDAY = datetime(2026, 10, 24, 6, 20, tzinfo=ZoneInfo("Europe/Kyiv"))
MONDAY = datetime(2026, 10, 26, 6, 20, tzinfo=ZoneInfo("Europe/Kyiv"))


def _row(label, asset_class):
    return {
        "asset_id": label.lower(), "label": label, "asset_class": asset_class,
        "price": Decimal("100"), "observed_at": date(2026, 10, 23), "change_pct": Decimal("1"),
        "change_days": 1, "source": "yahoo_chart", "staleness_days": 1,
        "is_stale": False, "is_volatile": False,
    }


def test_watchlist_on_saturday_shows_crypto_and_lists_closed_markets():
    rows = [_row("BTC", "crypto"), _row("Золото", "commodity"), _row("EUR/USD", "fx")]
    open_rows, closed = split_by_market(rows, SATURDAY)
    assert [r["label"] for r in open_rows] == ["BTC"]
    text = format_message(open_rows, closed)
    assert "BTC" in text
    assert "💤 Ринок закритий (ціни з останньої сесії): Золото, EUR/USD" in text


def test_watchlist_on_monday_is_unchanged():
    rows = [_row("BTC", "crypto"), _row("Золото", "commodity")]
    open_rows, closed = split_by_market(rows, MONDAY)
    assert len(open_rows) == 2 and closed == []
    assert "💤" not in format_message(open_rows, closed)


def test_news_on_saturday_keeps_geopolitics_and_crypto_only():
    classes = {"btc": "crypto", "xauusd": "commodity"}
    rows = [
        {"id": 1, "asset_id": None},
        {"id": 2, "asset_id": "btc"},
        {"id": 3, "asset_id": "xauusd"},
        {"id": 4, "asset_id": "META"},
    ]
    assert [r["id"] for r in drop_closed_markets(rows, classes, SATURDAY)] == [1, 2]
    assert len(drop_closed_markets(rows, classes, MONDAY)) == 4


def test_verdict_message_lists_assets_and_marks_unchanged():
    verdict = {
        "verdict_date": date(2026, 10, 23), "summary": "Долар тисне на метали.",
        "key_points": ["Золото: реальні ставки"], "conflicts": ["Нафта: запаси vs ОПЕК"], "watch": [],
    }
    analyses = [
        {"asset_id": "xauusd", "company_name": "Золото", "direction": "down", "confidence": 0.6,
         "summary": "Тиск ставок.", "created_at": datetime(2026, 10, 23, tzinfo=timezone.utc),
         "notified_at": None},
        {"asset_id": "wti_crude", "company_name": "Нафта <WTI>", "direction": "up", "confidence": 0.5,
         "summary": "Запаси падають.", "created_at": datetime(2026, 10, 20, tzinfo=timezone.utc),
         "notified_at": datetime(2026, 10, 20, tzinfo=timezone.utc)},
    ]
    text = format_verdict_message(verdict, analyses)
    assert "🧭 Фундаментальний вердикт" in text
    assert "Суперечності:" in text and "На що зважати:" not in text
    assert "Золото</b> · 0.60 — Тиск ставок." in text
    assert "(без змін з 20.10)" in text
    # HTML-екранування назви
    assert "Нафта &lt;WTI&gt;" in text


def test_weekend_wrap_message():
    text = format_wrap({
        "wrap_date": date(2026, 10, 25), "summary": "Тихі вихідні.", "direction": "down",
        "confidence": 0.55, "gap_risks": ["Нафта — удари по танкерах"], "crypto": "BTC −3%.",
        "watch_monday": ["Відкриття Токіо"],
    })
    assert "🗓 Підсумок вихідних · 2026-10-25" in text
    assert "risk-off" in text
    assert "⚡ Нафта — удари по танкерах" in text
    assert "👁 Відкриття Токіо" in text
