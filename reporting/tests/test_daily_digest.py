from datetime import date
from decimal import Decimal

import pytest

import daily_digest
from daily_digest import (
    fetch_recent_candidates,
    fetch_recent_market_synthesis,
    fetch_recent_news_synthesis,
    fetch_recent_surprises,
    format_candidate_message,
    format_market_synthesis_message,
    format_news_synthesis_message,
    format_surprise_message,
)


def test_format_news_synthesis_message():
    row = {
        "asset_id": "xauusd",
        "cluster_count": 3,
        "net_lean": 2,
        "price_pct_change": Decimal("-1.64"),
        "price_start_date": date(2026, 9, 23),
        "price_end_date": date(2026, 9, 30),
        "direction": "down",
        "confidence": Decimal("0.55"),
        "summary": "Ціна впала на 1.64%, новинний сигнал змішаний.",
        "confirmation_factors": "Якщо падіння продовжиться без нових ведмежих новин — це вже не корекція.",
    }
    text = format_news_synthesis_message(row)
    assert "xauusd" in text
    assert "-1.64%" in text
    assert "2026-09-23" in text and "2026-09-30" in text
    assert "+2" in text
    assert "3 історій" in text
    assert "Ціна впала на 1.64%" in text
    assert "Перевірити: Якщо падіння продовжиться" in text


def test_format_news_synthesis_message_omits_confirmation_line_when_absent():
    row = {
        "asset_id": "wti_crude",
        "cluster_count": 1,
        "net_lean": 0,
        "price_pct_change": Decimal("0.0"),
        "price_start_date": date(2026, 9, 23),
        "price_end_date": date(2026, 9, 30),
        "direction": "neutral",
        "confidence": Decimal("0.3"),
        "summary": "Без руху.",
        "confirmation_factors": None,
    }
    text = format_news_synthesis_message(row)
    assert "Перевірити:" not in text


def test_format_market_synthesis_message():
    row = {"cluster_count": 8, "direction": "down", "confidence": Decimal("0.55"), "summary": "Risk-off стан."}
    text = format_market_synthesis_message(row)
    assert "risk-off" in text
    assert "0.55" in text
    assert "Risk-off стан." in text


def test_format_market_synthesis_message_unknown_direction_falls_back_to_raw():
    row = {"cluster_count": 1, "direction": "something_new", "confidence": Decimal("0.5"), "summary": "x"}
    text = format_market_synthesis_message(row)
    assert "something_new" in text


def test_format_surprise_message():
    row = {
        "metric_id": "cpi",
        "observed_at": date(2026, 9, 30),
        "actual_value": 0.397,
        "expected_value_raw": "0.4%",
        "expected_value_parsed": 0.4,
        "surprise": -0.003,
        "surprise_pct": -0.75,
    }
    text = format_surprise_message(row)
    assert "CPI (інфляція, США)" in text
    assert "0.397" in text
    assert "0.4%" in text


def test_format_surprise_message_omits_pct_when_none():
    row = {
        "metric_id": "unemployment_rate",
        "observed_at": date(2026, 9, 30),
        "actual_value": 4.1,
        "expected_value_raw": "4.1%",
        "expected_value_parsed": 4.1,
        "surprise": 0.0,
        "surprise_pct": None,
    }
    text = format_surprise_message(row)
    assert "%)" not in text.split("Сюрприз:")[1]


def test_format_candidate_message():
    row = {
        "ticker": "ACME", "company_name": "Acme Robotics Inc.",
        "reasoning": "рух -11.2% за 5 торгових днів", "direction": "down",
        "asset_class": "stock",
    }
    text = format_candidate_message(row)
    assert "ACME" in text
    assert "Acme Robotics Inc." in text
    assert "рух -11.2%" in text
    # 2026-10-05: напрямок руху в дайджесті теж видно — інакше рядок
    # читався б як "ось вам тикер" без головної інформації.
    assert "↓" in text


# --- Regression 2026-09-28: daily_digest дублював notify_synthesis/
# notify_market_synthesis/notify_candidates/notify_expectations символ-в-
# символ, бо читав джерела сліпо за `created_at`-вікном, без урахування
# того, що окремі notify_*-джоби вже надіслали ці самі рядки й
# позначили notified_at. Тести нижче перевіряють САМЕ це: реальний SQL
# fetch_*-функцій і те, що main() дійсно викликає mark_notified/
# mark_expectations_notified (не просто форматує повідомлення).


class _FakeCursor:
    """Той самий підхід, що reporting/tests/test_news_notify.py:
    _FakeCursor — записує виконаний SQL, не звертається до реальної БД."""

    description = []  # порожній SELECT-результат — ці тести дивляться лише на виконаний SQL

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params=()):
        self._conn.executed.append((query, params))

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self):
        self.executed = []
        self.committed = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed += 1

    def close(self):
        pass


@pytest.mark.parametrize(
    "fetch_fn, extra_args",
    [
        (fetch_recent_news_synthesis, ()),
        (fetch_recent_market_synthesis, ()),
        (fetch_recent_surprises, ()),
        (fetch_recent_candidates, ()),
    ],
)
def test_fetch_functions_read_whole_window_regardless_of_notified(fetch_fn, extra_args):
    """ПЕРЕПИСАНО 2026-10-13. Раніше тест закріплював протилежне — що
    кожне з 4 джерел виключає вже надіслані рядки. На живих даних це
    означало, що дайджест не бачить НІЧОГО: десять окремих notify-джоб
    проходять раніше й позначають усе надісланим. Вивід 12 жовтня —
    о 20:00 надіслано "Сьогодні суттєвих подій не було" в день, коли
    Brent зробив +5% на ракетному удару по Ер-Ріяду.

    Дайджест — ЗВЕДЕННЯ дня (Ціль 5), не черга доставки залишків,
    тому читає все вікно."""
    conn = _FakeConn()
    fetch_fn(conn, 24, *extra_args)
    assert len(conn.executed) == 1
    query, params = conn.executed[0]
    assert "notified_at IS NULL" not in query
    assert params[0] == 24


def _news_row(id=1):
    return {
        "id": id, "asset_id": "xauusd", "cluster_count": 1, "net_lean": 1,
        "price_pct_change": Decimal("-1.33"),
        "price_start_date": date(2026, 9, 23), "price_end_date": date(2026, 9, 30),
        "direction": "up", "confidence": Decimal("0.6"),
        "summary": "Бичача новина суперечить падінню ціни.",
        "confirmation_factors": "Перевірити, чи рух продовжиться без нових новин.",
    }


def _market_row(id=2):
    return {"id": id, "cluster_count": 8, "direction": "neutral", "confidence": Decimal("0.35"), "summary": "Збалансовано."}


def _surprise_row(id=3):
    return {
        "id": id, "metric_id": "cpi", "observed_at": date(2026, 9, 28), "actual_value": 0.4,
        "expected_value_raw": "0.3%", "expected_value_parsed": 0.3, "surprise": 0.1, "surprise_pct": 33.3,
    }


def _candidate_row(id=4):
    return {
        "id": id, "ticker": "ACME", "company_name": "Acme Robotics",
        "reasoning": "рух +9.1% за 5 торгових днів", "direction": "up",
        "asset_class": "stock",
    }


def _patch_digest_sources(monkeypatch, conn, sent, *, news=(), market=None, surprises=(), candidates=()):
    monkeypatch.setattr(daily_digest, "resolve_telegram_credentials", lambda: ("token", "chat"))
    monkeypatch.setattr(daily_digest, "get_connection", lambda: conn)
    monkeypatch.setattr(
        daily_digest, "send_telegram_message",
        lambda token, chat_id, text, parse_mode=None: sent.append(text),
    )
    monkeypatch.setattr(daily_digest, "fetch_recent_news_synthesis", lambda conn, hours: list(news))
    monkeypatch.setattr(daily_digest, "fetch_recent_market_synthesis", lambda conn, hours: market)
    monkeypatch.setattr(daily_digest, "fetch_recent_surprises", lambda conn, hours: list(surprises))
    monkeypatch.setattr(daily_digest, "fetch_recent_candidates", lambda conn, hours: list(candidates))
    monkeypatch.setattr("sys.argv", ["daily_digest.py"])


def test_main_sends_exactly_one_consolidated_message(monkeypatch):
    """ПЕРЕПИСАНО 2026-10-13. Раніше дайджест слав ОКРЕМЕ повідомлення
    на кожну позицію й позначав кожен рядок надісланим. Обидві
    властивості виявились хибними на живих даних:

    - окремі повідомлення: коли фільтр `notified_at IS NULL` прибрали
      (бо через нього дайджест не бачив нічого), він одразу надіслав
      **25 повідомлень** за один прогін — тобто з "порожній" став
      "потоком", а Ціль 5 просить "кілька СПРАВДІ важливих подій";
    - позначення надісланим: дайджест відбирав матеріал у наступних
      прогонів окремих notify-джоб, тобто лише переносив проблему.

    Тепер — ОДНЕ зведення з розділами, і нічого не споживається."""
    conn = _FakeConn()
    sent = []
    _patch_digest_sources(
        monkeypatch, conn, sent,
        news=[_news_row(1)], market=_market_row(2),
        surprises=[_surprise_row(3)], candidates=[_candidate_row(4)],
    )

    daily_digest.main()

    assert len(sent) == 1, "дайджест мусить бути ОДНИМ повідомленням"
    assert conn.executed == [], "дайджест не позначає рядки надісланими"
    assert conn.committed == 0


def test_digest_caps_each_section(monkeypatch):
    """Ліміт на розділ (config.DIGEST_MAX_ITEMS_PER_SECTION) — те, чого
    бракувало, коли прогін видав 25 повідомлень. Зайві позиції не
    зникають безслідно: показується "…ще N"."""
    import config

    many = [_news_row(i) for i in range(config.DIGEST_MAX_ITEMS_PER_SECTION + 4)]
    text, count = daily_digest.build_digest(
        market_row=None, news_rows=many, surprise_rows=[], candidate_rows=[], hours=24,
    )

    assert count == config.DIGEST_MAX_ITEMS_PER_SECTION
    assert "…ще 4" in text


def test_digest_reports_zero_items_for_empty_window():
    text, count = daily_digest.build_digest(
        market_row=None, news_rows=[], surprise_rows=[], candidate_rows=[], hours=24,
    )
    assert count == 0


def test_main_sends_fallback_and_marks_nothing_when_all_already_notified(monkeypatch):
    """Регресія: до фіксу цей сценарій був НЕДОСЯЖНИЙ — daily_digest
    завжди щось знаходив у 24-годинному вікні, навіть якщо notify_*-джоби
    вже все надіслали. Тепер порожній `notified_at IS NULL`-результат —
    штатний випадок, і жодного зайвого UPDATE не виконується."""
    conn = _FakeConn()
    sent = []
    _patch_digest_sources(monkeypatch, conn, sent)  # усі джерела порожні

    daily_digest.main()

    assert sent == ["Сьогодні суттєвих подій не було."]
    assert conn.executed == []
    assert conn.committed == 0


