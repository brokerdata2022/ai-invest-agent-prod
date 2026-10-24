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


def test_list_message_without_verdict_points_to_ask():
    from fundamental_notify import format_list_message

    text = format_list_message([
        {"asset_id": "btc", "company_name": "BTC", "direction": "up", "confidence": 0.5, "summary": "Ріст."},
    ])
    assert "🟢 <b>BTC</b> · 0.50 — Ріст." in text
    assert "/ask" in text


def test_mark_all_unnotified_clears_whole_queue():
    from fundamental_notify import mark_all_unnotified

    class _Cur:
        rowcount = 54

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self.sql = sql

    class _Conn:
        committed = False

        def cursor(self):
            self.cur = _Cur()
            return self.cur

        def commit(self):
            self.committed = True

    conn = _Conn()
    assert mark_all_unnotified(conn) == 54
    assert "WHERE notified_at IS NULL" in conn.cur.sql
    assert conn.committed


# --- розбір по чинниках абзацами (живий фідбек 2026-10-24) ---

def test_inline_numbered_reasoning_becomes_paragraphs_with_bold_labels():
    from fundamental_notify import format_asset_message

    text = format_asset_message({
        "asset_id": "xauusd", "asset_kind": "watchlist", "company_name": "Золото",
        "direction": "down", "confidence": 0.65, "summary": "Тиск.",
        "reasoning": "1) Ціна золота: знизилася з 4.18 до 4.01 тис. Імпульс униз. "
                     "2) Дохідність 10Y: 5.19% → 5.13%. Підсумок: домінують негативні чинники.",
        "strengths": [], "risks": [],
    })
    assert "\n\n▫️ <b>Ціна золота:</b> знизилася з 4.18 до 4.01 тис. Імпульс униз." in text
    assert "\n\n▫️ <b>Дохідність 10Y:</b> 5.19% → 5.13%." in text
    assert "\n\n▫️ <b>Підсумок:</b> домінують негативні чинники." in text


def test_raw_json_reasoning_is_split_into_items():
    from fundamental_notify import split_reasoning

    items = split_reasoning('{"Ціна ETH: флет 2.40-2.44.","Обсяг торгів: падіння вшестеро."}')
    assert items == ["Ціна ETH: флет 2.40-2.44.", "Обсяг торгів: падіння вшестеро."]


def test_sentences_with_factor_labels_are_split():
    from fundamental_notify import split_reasoning

    items = split_reasoning("Ціна BTC: коридор 81.49-82.42 тис., відскок. Обсяг торгів: падіння. Капіталізація: 1655 млрд.")
    assert [i.split(":")[0] for i in items] == ["Ціна BTC", "Обсяг торгів", "Капіталізація"]


def test_new_line_separated_reasoning_is_kept_as_is():
    from fundamental_notify import split_reasoning

    assert split_reasoning("Ціна: впала на 4%\nПідсумок: тиск униз") == ["Ціна: впала на 4%", "Підсумок: тиск униз"]
