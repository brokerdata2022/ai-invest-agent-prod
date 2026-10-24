"""
Тести watchlist_db.py — на фейковому conn/cursor (той самий принцип,
що test_scheduler_heartbeat.py/test_telegram_commands.py), без реальної
БД. fetch_watchlist()/find_asset() перевіряються через SQL-результат,
add_asset()/set_enabled() — через факт виконаного запиту+commit.
"""

from datetime import date, timedelta

import common.watchlist_db as watchlist_db
from common.watchlist_db import SourceCandidate


# Набір полів за замовчуванням = watchlist_db._COLUMNS.
_DEFAULT_COLUMNS = [
    ("asset_id",), ("source",), ("metric_id",), ("ticker",), ("label",),
    ("search_term",), ("search_term_status",), ("enabled",),
]


class _FakeCursor:
    def __init__(self, rows, log, columns=None):
        self._rows = rows
        self._log = log
        self.rowcount = 1 if rows else 0
        # `search_term_status` у наборі з 2026-10-06 — без нього
        # fetch_terms() тихо повертав би ПОРОЖНІЙ словник (фільтр за
        # статусом не знаходив би поля). Запити з іншим набором полів
        # (fetch_pending_terms) передають `columns` явно.
        self.description = columns or _DEFAULT_COLUMNS

    def execute(self, sql, params=None):
        self._log.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, rows=(), columns=None):
        self.log = []
        self.committed = False
        self._rows = list(rows)
        self._columns = columns

    def cursor(self):
        return _FakeCursor(self._rows, self.log, columns=self._columns)

    def commit(self):
        self.committed = True


# Порядок полів = watchlist_db._COLUMNS (із 2026-10-06 там є
# search_term_status перед enabled).
_XAUUSD_ROW = ("xauusd", "twelvedata", "xauusd_close", "XAU/USD", "Золото (XAU/USD)",
               '"gold price"', "validated", True)
_BTC_ROW = ("btc", "binance", "btc_close", None, "BTC/USDT", "Bitcoin", "validated", True)
# search_term=None — 2026-10-04, живий кейс "BNB": термін не пройшов
# live-перевірку GDELT, актив збирає лише ціну.
_BNB_NO_TERM_ROW = ("bnb", "binance", "bnb_close", "BNBUSDT", "BNB/USDT", None,
                    "rejected", True)
# 2026-10-06: термін ЗБЕРЕЖЕНО, але перевірку не вдалось провести —
# у спільний запит не йде, джоба revalidate_news_terms спробує знову.
_BNB_PENDING_ROW = ("bnb", "binance", "bnb_close", "BNBUSDT", "BNB/USDT", '"BNB crypto"',
                    "pending", True)


def test_fetch_watchlist_returns_list_of_dicts():
    conn = _FakeConn(rows=[_XAUUSD_ROW, _BTC_ROW])
    rows = watchlist_db.fetch_watchlist(conn)

    assert len(rows) == 2
    assert rows[0]["asset_id"] == "xauusd"
    assert rows[0]["ticker"] == "XAU/USD"
    assert rows[1]["asset_id"] == "btc"


def test_fetch_watchlist_enabled_only_adds_where_clause():
    conn = _FakeConn(rows=[_XAUUSD_ROW])
    watchlist_db.fetch_watchlist(conn, enabled_only=True)

    sql, _ = conn.log[0]
    assert "WHERE enabled" in sql


def test_fetch_watchlist_all_omits_where_clause():
    conn = _FakeConn(rows=[_XAUUSD_ROW])
    watchlist_db.fetch_watchlist(conn, enabled_only=False)

    sql, _ = conn.log[0]
    assert "WHERE" not in sql


def test_fetch_asset_ids_returns_plain_list():
    conn = _FakeConn(rows=[_XAUUSD_ROW, _BTC_ROW])
    assert watchlist_db.fetch_asset_ids(conn) == ["xauusd", "btc"]


def test_fetch_terms_maps_asset_id_to_search_term():
    conn = _FakeConn(rows=[_XAUUSD_ROW])
    assert watchlist_db.fetch_terms(conn) == {"xauusd": '"gold price"'}


def test_fetch_terms_skips_rows_with_null_search_term():
    # 2026-10-04: один поганий GDELT-термін ламав ЦІЛИЙ watchlist-запит
    # (той самий OR для всіх активів) — рядки з search_term=NULL
    # (не пройшли live-перевірку при додаванні) не потрапляють у query.
    conn = _FakeConn(rows=[_XAUUSD_ROW, _BNB_NO_TERM_ROW])
    assert watchlist_db.fetch_terms(conn) == {"xauusd": '"gold price"'}
    assert "bnb" not in watchlist_db.fetch_terms(conn)


def test_fetch_terms_skips_pending_terms():
    """2026-10-06: термін, який ЗБЕРЕЖЕНО, але ще не перевірено, у
    спільний запит іти не має — інакше одна неперевірена фраза могла б
    зіпсувати новини для ВСІХ активів (причина фільтра 2026-10-04)."""
    conn = _FakeConn(rows=[_XAUUSD_ROW, _BNB_PENDING_ROW])
    assert watchlist_db.fetch_terms(conn) == {"xauusd": '"gold price"'}


def test_fetch_pending_terms_returns_only_pending():
    # Цей запит віддає ТРИ поля, не весь _COLUMNS.
    conn = _FakeConn(
        rows=[("bnb", "BNB/USDT", '"BNB crypto"')],
        columns=[("asset_id",), ("label",), ("search_term",)],
    )
    pending = watchlist_db.fetch_pending_terms(conn)
    assert pending == [{"asset_id": "bnb", "label": "BNB/USDT", "search_term": '"BNB crypto"'}]
    sql, params = conn.log[0]
    assert "search_term_status = %s" in sql
    assert params == (watchlist_db.TERM_PENDING,)


def test_set_term_status_updates_and_commits():
    conn = _FakeConn()
    watchlist_db.set_term_status(conn, "bnb", watchlist_db.TERM_VALIDATED)
    assert conn.committed is True
    sql, params = conn.log[0]
    assert "search_term_status = %s" in sql
    assert params == ("validated", "bnb")


def test_set_term_status_rejects_unknown_status():
    """Друкарська помилка в статусі тихо вивела б актив із новинного
    запиту назавжди — краще виняток."""
    import pytest

    conn = _FakeConn()
    with pytest.raises(ValueError):
        watchlist_db.set_term_status(conn, "bnb", "вигадка")


def test_fetch_price_sources_maps_asset_id_to_source_and_metric_id():
    conn = _FakeConn(rows=[_XAUUSD_ROW, _BTC_ROW])
    assert watchlist_db.fetch_price_sources(conn) == {
        "xauusd": ("twelvedata", "xauusd_close"),
        "btc": ("binance", "btc_close"),
    }


def test_find_asset_returns_row_when_present():
    conn = _FakeConn(rows=[_XAUUSD_ROW])
    row = watchlist_db.find_asset(conn, "xauusd")
    assert row["asset_id"] == "xauusd"


def test_find_asset_returns_none_when_absent():
    conn = _FakeConn(rows=[])
    assert watchlist_db.find_asset(conn, "gbpusd") is None


def test_add_asset_inserts_and_commits():
    conn = _FakeConn()
    watchlist_db.add_asset(
        conn, asset_id="gbpusd", source="twelvedata", metric_id="gbpusd_close",
        ticker="GBP/USD", label="GBP/USD", search_term='"GBP/USD"',
    )

    assert conn.committed is True
    sql, params = conn.log[0]
    assert "INSERT INTO watchlist_assets" in sql
    assert params == ("gbpusd", "twelvedata", "gbpusd_close", "GBP/USD", "GBP/USD",
                      '"GBP/USD"', "validated")


def test_add_asset_accepts_null_search_term():
    # 2026-10-04: актив без придатного GDELT-терміну все одно додається
    # (ціна збирається), просто search_term=None у INSERT.
    conn = _FakeConn()
    watchlist_db.add_asset(
        conn, asset_id="bnb", source="binance", metric_id="bnb_close",
        ticker="BNBUSDT", label="BNB/USDT", search_term=None,
        search_term_status="rejected",
    )

    sql, params = conn.log[0]
    assert params == ("bnb", "binance", "bnb_close", "BNBUSDT", "BNB/USDT", None, "rejected")


def test_add_asset_stores_pending_term_for_later_check():
    """2026-10-06, корінь живого фідбеку: термін, який не вдалось
    перевірити, МУСИТЬ зберегтись — інакше його ніколи не перевірять
    знову, і актив лишиться без новин назавжди."""
    conn = _FakeConn()
    watchlist_db.add_asset(
        conn, asset_id="bnb", source="binance", metric_id="bnb_close",
        ticker="BNBUSDT", label="BNB/USDT", search_term='"BNB crypto"',
        search_term_status=watchlist_db.TERM_PENDING,
    )

    _, params = conn.log[0]
    assert params[-2:] == ('"BNB crypto"', "pending")


def test_set_search_term_updates_and_commits():
    conn = _FakeConn(rows=[_BNB_NO_TERM_ROW])  # rowcount=1 через непорожні rows
    assert watchlist_db.set_search_term(conn, "bnb", '"BNB crypto"') is True
    assert conn.committed is True
    sql, params = conn.log[0]
    assert "UPDATE watchlist_assets" in sql
    assert params == ('"BNB crypto"', "bnb")


def test_set_search_term_accepts_none_to_clear():
    conn = _FakeConn(rows=[_BNB_NO_TERM_ROW])
    assert watchlist_db.set_search_term(conn, "bnb", None) is True


def test_set_search_term_returns_false_when_asset_missing():
    conn = _FakeConn(rows=[])
    assert watchlist_db.set_search_term(conn, "unknown", '"x"') is False


def test_set_source_updates_source_metric_id_and_ticker():
    # 2026-10-04: перемикання джерела (fred -> tradingeconomics і т.п.)
    # тепер звичайна операція над даними, не зміна схеми (живий урок —
    # забутий apply_schema.py після UPDATE у db/schema.sql дав "фікс",
    # що нічого не змінив у живій БД).
    conn = _FakeConn(rows=[_XAUUSD_ROW])
    assert watchlist_db.set_source(conn, "natgas", "tradingeconomics", metric_id="natgas", ticker="natural-gas") is True
    assert conn.committed is True
    sql, params = conn.log[0]
    assert "UPDATE watchlist_assets" in sql
    assert "source" in sql and "metric_id" in sql and "ticker" in sql
    assert params == ("tradingeconomics", "natgas", "natural-gas", "natgas")


def test_set_source_without_metric_id_or_ticker_only_changes_source():
    conn = _FakeConn(rows=[_XAUUSD_ROW])
    watchlist_db.set_source(conn, "natgas", "tradingeconomics")
    sql, params = conn.log[0]
    assert "metric_id" not in sql and "ticker" not in sql
    assert params == ("tradingeconomics", "natgas")


def test_set_source_returns_false_when_asset_missing():
    conn = _FakeConn(rows=[])
    assert watchlist_db.set_source(conn, "unknown", "tradingeconomics") is False


# --- choose_freshest_source() (2026-10-04, живий фідбек користувача:
# "резервний варіант має вже працювати... коли основне джерело не
# отримує свіжі дані то використовувати резерв") — потребує кількох
# РІЗНИХ відповідей курсора поспіль (MAX(observed_at) на кожного
# кандидата, потім find_asset, потім, можливо, set_source), тому
# окремий "покроковий" фейк, не _FakeConn вище (той повертає ОДНІ й ті
# самі rows з кожного cursor()).


class _StepCursor:
    def __init__(self, fetchone_result, description=None):
        self._result = fetchone_result
        self.description = description or []
        self.rowcount = 1 if fetchone_result else 0
        self.executed = None

    def execute(self, sql, params=None):
        self.executed = (" ".join(sql.split()), params)

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_ASSET_COLUMNS = [("asset_id",), ("source",), ("metric_id",), ("ticker",), ("label",), ("search_term",), ("enabled",)]


class _SequencedConn:
    """`steps` — список (fetchone_result, description) у ПОРЯДКУ
    викликів cursor() всередині функції, що тестується."""

    def __init__(self, steps):
        self._steps = list(steps)
        self.cursors = []
        self.committed = False

    def cursor(self):
        fetchone_result, description = self._steps.pop(0)
        cur = _StepCursor(fetchone_result, description)
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.committed = True


_TODAY = date.today()
_STALE_DATE = _TODAY - timedelta(days=10)  # гарантовано >1 будній день за будь-якого вирівнювання тижня

_TE_CANDIDATE = SourceCandidate("tradingeconomics", "xauusd", "gold")
_TD_CANDIDATE = SourceCandidate("twelvedata", "xauusd", "XAU/USD")

_TE_ASSET_ROW = ("xauusd", "tradingeconomics", "xauusd", "gold", "Золото", '"gold price"', True)


def test_choose_freshest_source_keeps_primary_when_fresh():
    conn = _SequencedConn(
        steps=[
            ((_TODAY,), None),  # MAX(observed_at) для tradingeconomics -- свіже
            (_TE_ASSET_ROW, _ASSET_COLUMNS),  # find_asset -- вже tradingeconomics, не перемикати
        ]
    )
    chosen = watchlist_db.choose_freshest_source(conn, "xauusd", [_TE_CANDIDATE, _TD_CANDIDATE])

    assert chosen == _TE_CANDIDATE
    assert len(conn.cursors) == 2  # лише 1 MAX-запит (primary вже свіжий) + find_asset, TD НЕ запитувався
    assert conn.committed is False  # set_source НЕ викликався


def test_choose_freshest_source_switches_to_fresh_fallback():
    conn = _SequencedConn(
        steps=[
            ((_STALE_DATE,), None),  # tradingeconomics -- застарілий
            ((_TODAY,), None),  # twelvedata -- свіжий
            (_TE_ASSET_ROW, _ASSET_COLUMNS),  # find_asset -- поточне джерело tradingeconomics
            (None, None),  # set_source(): UPDATE, fetchone() тут не викликається, але крок має бути в черзі
        ]
    )
    chosen = watchlist_db.choose_freshest_source(conn, "xauusd", [_TE_CANDIDATE, _TD_CANDIDATE])

    assert chosen == _TD_CANDIDATE
    update_sql, update_params = conn.cursors[-1].executed
    assert "UPDATE watchlist_assets" in update_sql
    assert "twelvedata" in update_params
    assert conn.committed is True


def test_choose_freshest_source_falls_back_to_first_when_none_fresh():
    conn = _SequencedConn(
        steps=[
            ((_STALE_DATE,), None),  # tradingeconomics -- застарілий
            ((_STALE_DATE,), None),  # twelvedata -- теж застарілий
            (_TE_ASSET_ROW, _ASSET_COLUMNS),  # find_asset -- вже tradingeconomics (candidates[0])
        ]
    )
    chosen = watchlist_db.choose_freshest_source(conn, "xauusd", [_TE_CANDIDATE, _TD_CANDIDATE])

    # Жоден кандидат не свіжий -- чесний статус-кво (перший, не вдавана свіжість).
    assert chosen == _TE_CANDIDATE
    assert conn.committed is False


def test_choose_freshest_source_never_collected_treated_as_not_fresh():
    conn = _SequencedConn(
        steps=[
            ((None,), None),  # tradingeconomics -- ще жодного спостереження
            ((_TODAY,), None),  # twelvedata -- свіжий
            (_TE_ASSET_ROW, _ASSET_COLUMNS),
            (None, None),
        ]
    )
    chosen = watchlist_db.choose_freshest_source(conn, "xauusd", [_TE_CANDIDATE, _TD_CANDIDATE])
    assert chosen == _TD_CANDIDATE


def test_set_enabled_returns_true_when_row_updated():
    conn = _FakeConn(rows=[_XAUUSD_ROW])  # rowcount=1 через непорожні rows (_FakeCursor)
    assert watchlist_db.set_enabled(conn, "xauusd", False) is True
    assert conn.committed is True


def test_set_enabled_returns_false_when_asset_missing():
    conn = _FakeConn(rows=[])  # rowcount=0
    assert watchlist_db.set_enabled(conn, "unknown", False) is False


# --- профіль активу (2026-10-22) ---

def test_price_chain_reads_ordered_candidates_from_row():
    """Замість `jobs.py:_FALLBACK_CHAINS`: ланцюжок — у рядку активу."""
    row = {
        "asset_id": "eurusd", "source": "yahoo_chart", "metric_id": "eurusd", "ticker": "EURUSD=X",
        "price_chain": [
            {"source": "yahoo_chart", "metric_id": "eurusd", "ticker": "EURUSD=X"},
            {"source": "twelvedata", "metric_id": "eurusd_close", "ticker": "EUR/USD"},
        ],
    }
    assert watchlist_db.price_chain(row) == [
        SourceCandidate("yahoo_chart", "eurusd", "EURUSD=X"),
        # metric_id — той, ПІД ЯКИМ ряд лежить у БД ('_close'): доти
        # тут стояло 'eurusd', і резерв Twelve Data ніколи не брав
        # участі ні у звірці, ні у виборі джерела.
        SourceCandidate("twelvedata", "eurusd_close", "EUR/USD"),
    ]


def test_price_chain_falls_back_to_row_source_when_empty():
    row = {"asset_id": "btc", "source": "binance", "metric_id": "btc_close", "ticker": "BTCUSDT", "price_chain": None}
    assert watchlist_db.price_chain(row) == [SourceCandidate("binance", "btc_close", "BTCUSDT")]


def test_set_profile_writes_chain_as_jsonb_and_commits():
    conn = _FakeConn(rows=[("x",)])
    watchlist_db.set_profile(conn, "skyd", {
        "asset_class": "stock",
        "price_chain": [{"source": "twelvedata", "metric_id": "skyd_close", "ticker": "SKYD"}],
    })
    sql, params = conn.log[0]
    assert "asset_class = %s" in sql
    assert "price_chain = %s::jsonb" in sql
    assert params[0] == "stock"
    assert '"skyd_close"' in params[1]
    assert params[-1] == "skyd"
    assert conn.committed


def test_set_profile_rejects_unknown_field():
    """Тихо проігнорована колонка — рівно той клас дефектів, від якого
    профіль і рятує."""
    import pytest

    with pytest.raises(ValueError, match="asset_type"):
        watchlist_db.set_profile(_FakeConn(), "skyd", {"asset_type": "stock"})


def test_fetch_patterns_maps_asset_to_regexes_and_skips_empty():
    columns = [("asset_id",), ("news_patterns",)]
    conn = _FakeConn(rows=[("skyd", [r"\bskydance\b"]), ("mystery", None)], columns=columns)
    assert watchlist_db.fetch_patterns(conn, "news_patterns") == {"skyd": (r"\bskydance\b",)}


# --- видалення = видалення (2026-10-22, пункт 2 плану сесії) ---

def test_remove_asset_deletes_row_checklist_and_derived_analyses():
    conn = _FakeConn(rows=[_XAUUSD_ROW])
    removed = watchlist_db.remove_asset(conn, "xauusd")

    statements = [sql for sql, _ in conn.log]
    deleted = [s.split()[2] for s in statements if s.startswith("DELETE FROM")]
    assert deleted == [
        "asset_checklists", "fundamental_analysis", "news_synthesis",
        "news_consolidated", "trading_list", "llm_input_fingerprints", "watchlist_assets",
    ]
    # Список торгів — лише рядки, що прийшли зі списку обраних
    assert any("trading_list" in s and "source = 'watchlist'" in s for s in statements)
    assert set(removed) == {"asset_checklists", "fundamental_analysis", "news_synthesis",
                            "news_consolidated", "trading_list", "llm_input_fingerprints"}
    assert conn.committed


def test_remove_asset_never_touches_raw_data():
    """Правило 6: сирі дані не видаляються ніколи — повернутий актив
    одразу матиме історію."""
    conn = _FakeConn(rows=[_XAUUSD_ROW])
    watchlist_db.remove_asset(conn, "xauusd")
    joined = " ".join(sql for sql, _ in conn.log)
    for raw_table in ("raw_observations", "raw_news", "company_events", "insider_transactions",
                      "company_filings", "earnings_calendar", "decision_outcomes"):
        assert raw_table not in joined, raw_table


def test_remove_asset_returns_none_when_missing():
    assert watchlist_db.remove_asset(_FakeConn(rows=[]), "nope") is None
