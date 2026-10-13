from datetime import date, timedelta
from decimal import Decimal

from watchlist_notify import format_message


def _row(
    label, price="100.5", change_pct="1.23", observed_at=None, change_days=1,
    source="twelvedata", staleness_days=1, is_stale=False, is_volatile=False,
):
    return {
        "asset_id": label.lower(),
        "label": label,
        "price": Decimal(price),
        "observed_at": observed_at or date(2026, 10, 2),
        "change_pct": Decimal(change_pct) if change_pct is not None else None,
        "change_days": change_days if change_pct is not None else None,
        "source": source,
        "staleness_days": staleness_days,
        "is_stale": is_stale,
        "is_volatile": is_volatile,
    }


def test_format_message_empty():
    assert "немає" in format_message([]) or "не зібрані" in format_message([])


def test_format_message_known_rows():
    rows = [_row("Золото (XAU/USD)", "2650.30", "0.85"), _row("EUR/USD", "1.0862", "-0.12")]
    text = format_message(rows)

    assert "<b>Золото (XAU/USD)</b> — 2,650.30 (2026-10-02)" in text
    assert "24г: +0.85%" in text
    assert "<b>EUR/USD</b> — 1.0862 (2026-10-02)" in text
    assert "24г: -0.12%" in text
    # Тон: жодних прямих інструкцій купити/продати (reporting/CLAUDE.md).
    assert "купити" not in text.lower() and "продати" not in text.lower()


def test_format_message_missing_change_shows_na():
    rows = [_row("Кава", change_pct=None)]
    text = format_message(rows)
    assert "н/д" in text


def test_format_message_uses_4_decimals_for_small_values_and_2_for_large():
    rows = [_row("BTC/USDT", price="111234.5"), _row("EUR/USD", price="1.0862")]
    text = format_message(rows)

    assert "111,234.50" in text
    assert "1.0862" in text


def test_format_message_shows_real_gap_not_always_24h():
    # 2026-10-04, живий фідбек користувача: вихідні (FRED-товари/форекс
    # не оновлюються) і місячна кава показувались як "24г" — тепер
    # підпис відповідає РЕАЛЬНІЙ кількості днів між двома точками.
    rows = [
        _row("Нафта WTI", "96.16", "-3.23", change_days=3),  # п'ятниця -> понеділок
        _row("Кава", "359.16", "16.68", change_days=29),  # місячна серія
    ]
    text = format_message(rows)

    assert "3 дн.: -3.23%" in text
    assert "29 дн.: +16.68%" in text
    assert "24г" not in text


def test_format_message_marks_web_crosscheck_source():
    # 2026-10-04, живий фідбек користувача: "рішення з веб пошуком...
    # потрібно робити позначку що дані веб" — web_crosscheck/
    # tradingeconomics-рядки відрізняються міткою, API-рядки — ні.
    rows = [
        _row("Кава", "288.75", source="tradingeconomics"),
        _row("Золото", "4137.61", source="web_crosscheck"),
        _row("EUR/USD", "1.0862", source="twelvedata"),
    ]
    text = format_message(rows)

    assert "<b>Кава</b> 🌐 —" in text
    assert "<b>Золото</b> 🌐 —" in text
    assert "<b>EUR/USD</b> —" in text  # без мітки


def test_format_message_flags_stale_data():
    # 2026-10-04, живий фідбек користувача: "чого ти не перевіряєш
    # актуальність даних" — звіт тепер сам активно позначає рядок,
    # чия observed_at застаріла (common/freshness.py:is_stale(), БУДНІ
    # дні), не лише показує дату в дужках і сподівається, що хтось її
    # помітить. is_stale/staleness_days тут підставлені напряму в
    # _row() (fetch_watchlist_snapshot() — окремий, не тестований тут
    # шар, що й рахує їх через common/freshness.py).
    rows = [
        _row("Нафта WTI", "91.11", staleness_days=5, is_stale=True),
        _row("EUR/USD", "1.0862", staleness_days=1, is_stale=False),
    ]
    text = format_message(rows)

    assert "⚠️ застаріло (5дн)" in text
    assert "Застарілі дані" in text
    assert "Нафта WTI" in text.split("Застарілі дані")[1]
    assert "EUR/USD" not in text.split("Застарілі дані")[1]


def test_format_message_no_stale_section_when_all_fresh():
    rows = [_row("EUR/USD", "1.0862", staleness_days=1, is_stale=False)]
    text = format_message(rows)
    assert "Застарілі дані" not in text
    assert "⚠️" not in text


def test_format_message_flags_volatile_revisions():
    # 2026-10-04, живий кейс xauusd: Twelve Data переписав ВЖЕ закритий
    # день заднім числом на 2% — актив виглядав "свіжим" (is_stale=False),
    # але значення ненадійне. Користувач: "як я буду розуміти, чи дані
    # правильні — кожен раз перевіряти вручну?!" — тепер звіт сам
    # позначає це, не чекаючи, що користувач помітить розбіжність.
    rows = [
        _row("Золото (XAU/USD)", "4137.61", is_stale=False, is_volatile=True),
        _row("EUR/USD", "1.0862", is_stale=False, is_volatile=False),
    ]
    text = format_message(rows)

    assert "🔀 нестабільні ревізії" in text
    assert "🔀 Нестабільні ревізії (перевірте вручну): Золото (XAU/USD)" in text
    assert "EUR/USD" not in text.split("Нестабільні ревізії")[1]


def test_format_message_no_volatile_section_when_all_stable():
    rows = [_row("EUR/USD", "1.0862", is_volatile=False)]
    text = format_message(rows)
    assert "Нестабільні ревізії" not in text
    assert "🔀" not in text


# ============================================================
# "Остання сесія" проти "застаріло"
# ============================================================
# Живий фідбек користувача 2026-10-06: "ціна кави застаріла, чого не
# застосований резерв". Насправді `is_stale()` її застарілою НЕ
# вважав (1 робочий день), а 292.55 за 05.10 — це останнє закриття
# ICE: кава не торгується 24/7, на відміну від крипти й форексу. Але
# звіт показував голу дату, тож відрізнити "ринок закритий" від "наш
# збір відстав" було НЕМОЖЛИВО.


def test_yesterday_close_is_labelled_as_last_session_not_stale():
    rows = [_row("Кава", observed_at=date.today() - timedelta(days=1), staleness_days=1)]
    text = format_message(rows)

    assert "остання сесія" in text
    assert "застаріло" not in text


def test_today_price_has_no_session_label():
    """Актив, що торгується 24/7 (крипта), має дату сьогоднішню —
    жодних пояснень не потрібно, рядок лишається коротким."""
    text = format_message([_row("BTC/USDT", observed_at=date.today(), staleness_days=0)])
    assert "остання сесія" not in text


def test_genuinely_stale_still_gets_the_warning():
    """Межа лишається за `is_stale()`: 2+ робочих днів — це вже збій
    збору, і він мусить кричати, а не прикриватись "сесією"."""
    rows = [_row("Кава", observed_at=date.today() - timedelta(days=10),
                 staleness_days=10, is_stale=True)]
    text = format_message(rows)

    assert "застаріло" in text
    assert "остання сесія" not in text
