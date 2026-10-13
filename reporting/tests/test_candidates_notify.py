from datetime import datetime, timezone

import config
from candidates_notify import fetch_unnotified_candidates, format_message


def _row(**overrides):
    row = {
        "id": 1,
        "ticker": "ACME",
        "company_name": "Acme Robotics Inc.",
        "reasoning": "рух +12.4% за 5 торгових днів; обсяг до середнього +80%",
        "discovered_at": datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc),
        "asset_class": "stock",
        "direction": "up",
        "detector": "stock_movers",
        "score": "0.52",
    }
    row.update(overrides)
    return row


def test_format_message_empty():
    # 2026-09-28: дедуп-фікс змінив семантику з "поточний список порожній"
    # на "нічого нового з часу останнього сповіщення" — пуста БД і
    # "усе вже надіслано" тепер одна й та сама відповідь, це навмисно.
    assert "немає" in format_message([])


def test_format_message_known_rows():
    text = format_message([_row()])
    assert "ACME" in text
    assert "Acme Robotics Inc." in text
    assert "рух +12.4%" in text


def test_direction_is_visible_both_ways():
    """2026-10-05: напрямок — головне в цьому списку (падіння на обсязі
    й зростання на обсязі це різні події), тому він мусить бути в
    тексті, а не лише в БД."""
    up = format_message([_row(direction="up")])
    down = format_message([_row(direction="down", ticker="FALL")])
    assert "↑" in up
    assert "↓" in down


def test_classes_are_grouped():
    """Крипта рухається в іншому масштабі, ніж акції (8% за добу проти
    6% за тиждень) — змішаний список створював би хибне відчуття, що
    їх можна порівнювати напряму."""
    rows = [
        _row(asset_class="crypto", ticker="XUSDT", company_name="X/USDT (ф'ючерс)", detector="crypto_movers"),
        _row(asset_class="stock"),
    ]
    text = format_message(rows)
    assert "Крипта" in text and "Акції" in text
    assert text.index("Крипта") < text.index("XUSDT")


def test_unknown_class_is_not_lost():
    """Форекс/товари зʼявляться, коли детектори розширяться — такий
    рядок мусить дійти, а не зникнути з повідомлення."""
    text = format_message([_row(asset_class="fx", ticker="GBPUSD", company_name="GBP/USD")])
    assert "GBPUSD" in text


def test_no_trading_instruction_wording():
    """analysis/CLAUDE.md: ніде не "купити/продати"."""
    text = format_message([_row(), _row(direction="down", ticker="FALL")]).lower()
    for forbidden in ("купити", "продати", "входити в позицію"):
        assert forbidden not in text


def test_default_limit_matches_discovery_output():
    """Живий прогін 2026-10-05: пошук зберігав 12 кандидатів, а notify
    надсилав 10 — двоє залипали до наступного прогону. Ліміт
    повідомлення не має права бути нижчим за те, що пише analysis/."""
    default = fetch_unnotified_candidates.__defaults__[0]
    assert default >= config.DISCOVERY_MAX_ITEMS
