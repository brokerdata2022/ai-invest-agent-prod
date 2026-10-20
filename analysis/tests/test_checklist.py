"""
Чек-лист чинників для будь-якого активу (`fundamental/checklist.py`)
і позначки довіри факторних рядів (`fundamental/factors.py:trust_note`).

Головне, що тут стережеться: ШІ ПРОПОНУЄ, код ПЕРЕВІРЯЄ. Ключ, якого
немає в каталозі реальних рядів, не має потрапити в аналіз — інакше
чинник "з'явився б" у промпті без жодного числа, і LLM міг би
домислити його значення.
"""

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

import config
from fundamental import checklist as cl
from fundamental.factors import collect_asset_factors, trust_note

CATALOG = [
    cl.CatalogEntry("fred:fed_funds_rate", "Effective Fed Funds Rate", date(2026, 10, 17)),
    cl.CatalogEntry("yoy:fred:cpi", "CPI США → річна зміна", date(2026, 9, 1)),
    cl.CatalogEntry("asset:btc", "Ціна активу зі списку обраних: BTC", date(2026, 10, 19)),
    cl.CatalogEntry("coinmetrics:btc_tx_count", "Транзакцій BTC за добу", date(2026, 10, 19)),
]
KEYS = {e.key for e in CATALOG}
BNB = {"asset_id": "bnb", "label": "BNB/USDT", "source": "binance", "metric_id": "bnb_close"}


# --- розбір відповіді ШІ ---------------------------------------------

def test_parse_keeps_only_catalog_keys_and_reports_rejected():
    raw = json.dumps({
        "asset_class": "криптовалюта",
        "factors": [
            {"key": "asset:btc", "label": "BTC — бета крипторинку"},
            {"key": "bnb_chain:burn_rate", "label": "вигаданий ряд"},
            {"key": "fred:fed_funds_rate", "label": "ліквідність"},
        ],
        "gaps": ["спалювання BNB", "активність BNB Chain"],
    })
    chosen, gaps, rejected = cl.parse_response(raw, KEYS)

    assert [k for k, _ in chosen] == ["asset:btc", "fred:fed_funds_rate"]
    assert rejected == ["bnb_chain:burn_rate"]
    assert gaps == ["спалювання BNB", "активність BNB Chain"]


def test_parse_skips_reserved_and_duplicate_keys():
    """Ціна/новини/синтез додаються завжди — ШІ їх не обирає; дубль не
    подвоює вагу чинника в промпті."""
    raw = json.dumps({"factors": [
        {"key": "price"}, {"key": "news"}, {"key": "asset:btc"}, {"key": "asset:btc"},
    ], "gaps": []})
    chosen, _, rejected = cl.parse_response(raw, KEYS)
    assert [k for k, _ in chosen] == ["asset:btc"]
    assert rejected == []


def test_parse_caps_factor_count(monkeypatch):
    monkeypatch.setattr(config, "CHECKLIST_MAX_FACTORS", 2)
    raw = json.dumps({"factors": [{"key": k} for k in sorted(KEYS)], "gaps": []})
    chosen, _, _ = cl.parse_response(raw, KEYS)
    assert len(chosen) == 2


def test_parse_without_valid_keys_raises():
    raw = json.dumps({"factors": [{"key": "made:up"}], "gaps": ["щось"]})
    with pytest.raises(cl.ChecklistError, match="made:up"):
        cl.parse_response(raw, KEYS)


def test_parse_bad_json_raises_checklist_error():
    with pytest.raises(cl.ChecklistError):
        cl.parse_response("не json", KEYS)


def test_assemble_specs_wraps_with_price_news_synthesis():
    specs = cl.assemble_specs(BNB, [("asset:btc", ""), ("fred:fed_funds_rate", "ліквідність")],
                              {e.key: e.label for e in CATALOG})
    assert specs[0][0] == "price"
    assert specs[1] == ("asset:btc", "Ціна активу зі списку обраних: BTC"), "порожній підпис → опис з каталогу"
    assert specs[2] == ("fred:fed_funds_rate", "ліквідність")
    assert [k for k, _ in specs[-2:]] == ["news", "synthesis"]


# --- каталог ------------------------------------------------------------

class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append(params)

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self.cur = _Cursor(rows)

    def cursor(self):
        return self.cur


def test_catalog_offers_index_levels_only_as_yoy_and_prices_as_asset_keys():
    """Рівень CPI пропонується лише як `yoy:` (урок 2026-10-04); ціна
    іншого активу — як `asset:`, щоб пережити перемикання джерела; ціна
    самого активу в каталог не йде (вона і так завжди є)."""
    watchlist = [BNB, {"asset_id": "btc", "label": "BTC/USDT", "source": "yahoo_chart", "metric_id": "btc"}]
    conn = _Conn([
        ("fred", "cpi", date(2026, 9, 1)),
        ("fred", "fed_funds_rate", date(2026, 10, 17)),
        ("yahoo_chart", "btc", date(2026, 10, 19)),
        ("binance", "bnb_close", date(2026, 10, 19)),
        ("coinmetrics", "btc_tx_count", date(2026, 10, 19)),
        ("fred", "wti_crude", date(2026, 10, 19)),
    ])
    keys = {e.key for e in cl.build_catalog(conn, BNB, watchlist, today=date(2026, 10, 20))}

    assert "yoy:fred:cpi" in keys and "fred:cpi" not in keys
    assert "asset:btc" in keys
    assert "asset:bnb" not in keys
    assert "coinmetrics:btc_tx_count" in keys
    assert "fred:wti_crude" not in keys, "ціни товарів із FRED (Brent −33%) у каталог не йдуть"


def test_catalog_query_has_literal_date_bound():
    conn = _Conn([])
    cl.build_catalog(conn, BNB, [BNB], today=date(2026, 10, 20))
    since = conn.cur.executed[0][0]
    assert since == date(2026, 10, 20) - timedelta(days=config.CHECKLIST_CATALOG_MAX_AGE_DAYS)


def test_fingerprint_ignores_dates_but_not_keys():
    newer = [cl.CatalogEntry(e.key, e.label, date(2027, 1, 1)) for e in CATALOG]
    assert cl.catalog_fingerprint(CATALOG) == cl.catalog_fingerprint(newer)
    extra = CATALOG + [cl.CatalogEntry("eia:us_crude_stocks", "запаси", date(2026, 10, 9))]
    assert cl.catalog_fingerprint(CATALOG) != cl.catalog_fingerprint(extra)


# --- вирішення чек-листа ----------------------------------------------

@pytest.fixture
def wiring(monkeypatch):
    """Підміна БД/ШІ — рахуємо виклики генерації."""
    state = {"stored": None, "generated": 0, "saved": [], "fail": None}
    monkeypatch.setattr(cl, "build_catalog", lambda conn, asset, watchlist: CATALOG)
    monkeypatch.setattr(cl, "load_stored", lambda conn, asset_id: state["stored"])

    def fake_generate(conn, asset, catalog):
        state["generated"] += 1
        if state["fail"]:
            raise state["fail"]
        return [("asset:btc", "бета")], ["спалювання BNB"], [], 42

    monkeypatch.setattr(cl, "generate", fake_generate)
    monkeypatch.setattr(cl, "save", lambda conn, *args: state["saved"].append(args))
    return state


class _RollbackConn:
    def rollback(self):
        pass


def test_config_checklist_wins(wiring):
    result = cl.resolve_checklist(None, {"asset_id": "xauusd", "label": "Золото"}, [])
    assert result.origin == cl.ORIGIN_CONFIG
    assert wiring["generated"] == 0


def test_new_asset_gets_llm_checklist_and_it_is_stored(wiring):
    result = cl.resolve_checklist(None, BNB, [BNB])
    assert result.origin == cl.ORIGIN_LLM
    assert ("asset:btc", "бета") in result.specs
    assert result.gaps == ("спалювання BNB",)
    assert len(wiring["saved"]) == 1


def test_stored_checklist_reused_while_catalog_unchanged(wiring):
    """Не платимо за той самий чек-лист щодня, і аналіз не "плаває"."""
    wiring["stored"] = {
        "factors": [["fred:fed_funds_rate", "ліквідність"]], "gaps": [], "rejected": [],
        "catalog_fingerprint": cl.catalog_fingerprint(CATALOG),
    }
    result = cl.resolve_checklist(None, BNB, [BNB])
    assert wiring["generated"] == 0
    assert ("fred:fed_funds_rate", "ліквідність") in result.specs


def test_changed_catalog_triggers_regeneration(wiring):
    wiring["stored"] = {"factors": [["fred:fed_funds_rate", "x"]], "gaps": [], "rejected": [],
                        "catalog_fingerprint": "старий"}
    cl.resolve_checklist(None, BNB, [BNB])
    assert wiring["generated"] == 1


def test_llm_failure_falls_back_to_previous_then_default(wiring):
    wiring["fail"] = cl.ChecklistError("порожньо")
    assert cl.resolve_checklist(_RollbackConn(), BNB, [BNB]).origin == cl.ORIGIN_DEFAULT

    wiring["stored"] = {"factors": [["fred:fed_funds_rate", "x"]], "gaps": [], "rejected": [],
                        "catalog_fingerprint": "старий"}
    result = cl.resolve_checklist(_RollbackConn(), BNB, [BNB])
    assert result.origin == cl.ORIGIN_LLM, "застарілий чек-лист кращий за універсальний"


def test_dry_run_never_calls_llm(wiring):
    assert cl.resolve_checklist(None, BNB, [BNB], allow_generate=False).origin == cl.ORIGIN_DEFAULT
    assert wiring["generated"] == 0


def test_telegram_text_escapes_llm_labels():
    checklist = cl.Checklist("bnb", (("price", "Ціна"), ("asset:btc", "BTC <бета> & ризик")), ("x<y",), cl.ORIGIN_LLM)
    text = cl.format_checklist(BNB, checklist)
    assert "&lt;бета&gt; &amp; ризик" in text
    assert "x&lt;y" in text


# --- позначка довіри ------------------------------------------------------

def test_trust_note_agree_names_partner_and_spread():
    note = trust_note(
        "us_crude_stocks",
        {"observed_at": date(2026, 10, 9), "status": "agree", "max_spread_pct": Decimal("0.02"),
         "source_values": {}, "detail": ""},
        date(2026, 10, 9), today=date(2026, 10, 15),
    )
    assert note == "звірено з tradingeconomics на 2026-10-09: згода, розкид 0.02%"


def test_trust_note_disagreement_is_loud():
    note = trust_note(
        "btc_tx_count",
        {"observed_at": date(2026, 10, 14), "status": "disagree", "max_spread_pct": Decimal("9"),
         "source_values": {"coinmetrics": "700000", "blockchain_info": "840000"}, "detail": ""},
        date(2026, 10, 19), today=date(2026, 10, 20),
    )
    assert note.startswith("ДЖЕРЕЛА РОЗІЙШЛИСЬ")
    assert "blockchain_info=840000" in note


def test_trust_note_single_source_and_stale():
    """Одне джерело — не приховується; тиша Farside тиждень — ЗАСТАРІЛО."""
    note = trust_note("btc_etf_flow_usd_m", None, date(2026, 10, 9), today=date(2026, 10, 20))
    assert "ЗАСТАРІЛО: остання точка 2026-10-09" in note
    assert "одне джерело (farside)" in note


def test_trust_note_without_verdict_yet():
    assert trust_note("eth_tx_count", None, date(2026, 10, 19), today=date(2026, 10, 20)) == "звірки ще не було"


# --- збір чинників за чек-листом ---------------------------------------

class _QueueCursor:
    def __init__(self, queue):
        self.queue = list(queue)
        self._rows = []

    def execute(self, query, params=None):
        self._rows = self.queue.pop(0) if self.queue else []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _QueueConn:
    def __init__(self, queue):
        self._cursor = _QueueCursor(queue)

    def cursor(self):
        return self._cursor


def test_asset_key_reads_current_price_source_of_other_asset():
    """`asset:btc` → поточне джерело BTC із watchlist_assets → його ряд."""
    checklist = cl.Checklist("bnb", (("asset:btc", "BTC"),), ("спалювання",), cl.ORIGIN_LLM)
    conn = _QueueConn([
        [("yahoo_chart", "btc")],                       # watchlist_assets
        [(date(2026, 10, 19), Decimal("81748"))],       # ряд ціни BTC
    ])
    result = collect_asset_factors(conn, "bnb", "BNB", "binance", "bnb_close", checklist=checklist)

    (factor,) = result.factors
    assert factor.points == [{"observed_at": "2026-10-19", "value": Decimal("81748")}]
    assert result.gaps == ("спалювання",)
    assert result.using_default_factors is False


def test_asset_key_for_removed_asset_is_named_missing():
    checklist = cl.Checklist("bnb", (("asset:gone", "Щось"),), (), cl.ORIGIN_LLM)
    result = collect_asset_factors(_QueueConn([[]]), "bnb", "BNB", "binance", "bnb_close", checklist=checklist)
    assert "немає в списку обраних" in result.factors[0].missing_reason


def test_default_checklist_marks_analysis_as_generic():
    result = collect_asset_factors(
        _QueueConn([]), "bnb", "BNB", "binance", "bnb_close", checklist=cl.default_checklist("bnb"),
    )
    assert result.using_default_factors is True
    assert config.DEFAULT_FACTORS_CAVEAT in result.gaps


def test_factor_series_key_gets_trust_note():
    # Дата — сьогоднішня, а не прибита: інакше через кілька днів точка
    # "застаріла б" і тест падав би від самого плину часу.
    today = date.today()
    checklist = cl.Checklist("eth", (("coinmetrics:eth_tx_count", "Транзакції ETH"),), (), cl.ORIGIN_CONFIG)
    conn = _QueueConn([
        [(today, Decimal("1748461"))],                  # ряд
        [(today, "agree", Decimal("0"), {}, "")],       # вердикт
    ])
    result = collect_asset_factors(conn, "eth", "ETH", "yahoo_chart", "eth", checklist=checklist)
    assert result.factors[0].note.startswith(f"звірено з etherscan на {today}: згода")


def test_every_factor_series_key_in_asset_factors_exists():
    """Ключ факторного ряду в ручному чек-листі мусить вказувати на
    ОСНОВНЕ джерело ряду з config.FACTOR_SERIES — інакше чинник читав
    би джерело звірки (blockchain.info відстає на тиждень)."""
    sources = {spec["primary"] for spec in config.FACTOR_SERIES.values()}
    for asset_id, specs in config.ASSET_FACTORS.items():
        for key, _ in specs:
            if key.count(":") == 1 and key.split(":")[0] in sources:
                metric_id = key.split(":")[1]
                assert metric_id in config.FACTOR_SERIES, f"{asset_id}: {key}"
                assert config.FACTOR_SERIES[metric_id]["primary"] == key.split(":")[0], f"{asset_id}: {key}"


def test_trust_note_event_series_is_never_stale():
    """Подієвий ряд (інтервенції) без нових точок пів року — норма."""
    note = trust_note("japan_fx_intervention", None, date(2026, 5, 6), today=date(2026, 10, 20))
    assert "ЗАСТАРІЛО" not in note
    assert note == "одне джерело (mof_japan), незалежної звірки немає"
