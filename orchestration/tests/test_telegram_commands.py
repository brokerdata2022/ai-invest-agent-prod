"""
Тести telegram_commands.py — чиста логіка (parse_command/is_authorized/
format_*) без мережі/БД, і process_update()/get_offset()/save_offset()
на фейкових conn/cursor та підміненими launch_job/send_telegram_message
(той самий підхід, що test_scheduler_heartbeat.py)."""

from datetime import date
from decimal import Decimal

import telegram_commands
from telegram_commands import (
    _resolve_news_search_term,
    format_help,
    format_launch_ack,
    format_unknown_command,
    format_watchlist_list,
    get_offset,
    handle_watchlist_add,
    handle_watchlist_remove,
    is_authorized,
    normalize_ticker_to_asset_id,
    parse_command,
    parse_command_args,
    process_update,
    save_offset,
    validate_binance_symbol,
    validate_twelvedata_ticker,
)

import pytest


@pytest.fixture(autouse=True)
def _no_profile_network(monkeypatch):
    """`/watchlist_add` з 2026-10-22 ще й визначає профіль активу
    (тип у Twelve Data, CIK у SEC, друге джерело в Yahoo) — у тестах
    без мережі: за замовчуванням нічого не знайдено, тест, якому
    профіль потрібен, підміняє сам."""
    from common import profile_lookup

    monkeypatch.setattr(profile_lookup, "lookup_instrument", lambda *a, **k: None)
    monkeypatch.setattr(profile_lookup, "lookup_company", lambda *a, **k: None)
    monkeypatch.setattr(profile_lookup, "validate_yahoo_symbol", lambda *a, **k: False)


# --- parse_command ---

def test_parse_command_plain():
    assert parse_command("/notify_screening") == "notify_screening"


def test_parse_command_strips_bot_mention_and_args():
    assert parse_command("/notify_screening@MyCoolBot аргумент тут") == "notify_screening"


def test_parse_command_lowercases():
    assert parse_command("/Help") == "help"


def test_parse_command_not_a_command_returns_none():
    assert parse_command("просто текст") is None


def test_parse_command_empty_text_returns_none():
    assert parse_command("") is None


def test_parse_command_bare_slash_returns_none():
    assert parse_command("/") is None


# --- is_authorized ---

def test_is_authorized_matching_chat_id():
    assert is_authorized(123456, "123456") is True


def test_is_authorized_mismatched_chat_id():
    assert is_authorized(999, "123456") is False


# --- format_* ---

def test_format_help_lists_known_job_with_description():
    text = format_help()
    assert "/notify_screening" in text
    # "&" ескейпиться для Telegram HTML (escape_html) — у вихідному
    # тексті "S&amp;P 500", не сирий "S&P 500".
    assert "Надіслати скринінг S&amp;P 500" in text


def test_format_launch_ack_includes_job_name_and_description():
    text = format_launch_ack("notify_screening")
    assert "notify_screening" in text
    assert "Надіслати скринінг S&amp;P 500" in text


def test_format_unknown_command_mentions_help():
    text = format_unknown_command("not_a_real_job")
    assert "not_a_real_job" in text
    assert "/help" in text


# --- process_update ---

def _message(text, chat_id=123456):
    return {"update_id": 1, "message": {"text": text, "chat": {"id": chat_id}}}


def test_process_update_ignores_unauthorized_chat(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_commands, "send_telegram_message", lambda *a, **k: sent.append(a))
    launched = []
    monkeypatch.setattr(telegram_commands, "launch_job", lambda name: launched.append(name))

    process_update(_message("/notify_screening", chat_id=999), configured_chat_id="123456", token="T")

    assert sent == []
    assert launched == []


def test_process_update_launches_known_job(monkeypatch):
    sent = []
    launched = []
    monkeypatch.setattr(telegram_commands, "send_telegram_message", lambda token, chat_id, text, **k: sent.append(text))
    monkeypatch.setattr(telegram_commands, "launch_job", lambda name: launched.append(name))

    process_update(_message("/notify_screening"), configured_chat_id="123456", token="T")

    assert launched == ["notify_screening"]
    assert len(sent) == 1
    assert "notify_screening" in sent[0]


def test_process_update_help_command_does_not_launch_anything(monkeypatch):
    sent = []
    launched = []
    monkeypatch.setattr(telegram_commands, "send_telegram_message", lambda token, chat_id, text, **k: sent.append(text))
    monkeypatch.setattr(telegram_commands, "launch_job", lambda name: launched.append(name))

    process_update(_message("/help"), configured_chat_id="123456", token="T")

    assert launched == []
    assert len(sent) == 1
    assert "/notify_screening" in sent[0]


def test_process_update_unknown_command(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_commands, "send_telegram_message", lambda token, chat_id, text, **k: sent.append(text))
    monkeypatch.setattr(telegram_commands, "launch_job", lambda name: (_ for _ in ()).throw(AssertionError))

    process_update(_message("/not_a_real_job"), configured_chat_id="123456", token="T")

    assert len(sent) == 1
    assert "Невідома команда" in sent[0]


def test_process_update_rejects_technical_job_not_in_info_set(monkeypatch):
    # 2026-10-03: "check_releases" — реальна джоба (jobs.py:JOBS), але
    # технічна (не надсилає інформацію) — більше НЕ запускається через
    # Telegram, навіть якщо хтось набере її вручну (команда просто
    # невідома, той самий текст, що для вигаданого імені).
    sent = []
    monkeypatch.setattr(telegram_commands, "send_telegram_message", lambda token, chat_id, text, **k: sent.append(text))
    monkeypatch.setattr(telegram_commands, "launch_job", lambda name: (_ for _ in ()).throw(AssertionError))

    process_update(_message("/check_releases"), configured_chat_id="123456", token="T")

    assert len(sent) == 1
    assert "Невідома команда" in sent[0]


def test_process_update_ignores_plain_text(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_commands, "send_telegram_message", lambda *a, **k: sent.append(a))

    process_update(_message("привіт, як справи?"), configured_chat_id="123456", token="T")

    assert sent == []


def test_process_update_ignores_update_without_message():
    process_update({"update_id": 1, "edited_message": {}}, configured_chat_id="123456", token="T")  # не кидає виняток


def test_process_update_ignores_message_without_text():
    update = {"update_id": 1, "message": {"chat": {"id": 123456}, "sticker": {}}}
    process_update(update, configured_chat_id="123456", token="T")  # не кидає виняток


# --- watchlist: parse_command_args / normalize_ticker_to_asset_id ---

def test_parse_command_args_returns_rest_of_text():
    assert parse_command_args("/watchlist_add GBP/USD") == "GBP/USD"


def test_parse_command_args_strips_whitespace():
    assert parse_command_args("/watchlist_add   GBP/USD  ") == "GBP/USD"


def test_parse_command_args_empty_when_no_args():
    assert parse_command_args("/watchlist_list") == ""


def test_normalize_ticker_to_asset_id_strips_slash_and_lowercases():
    assert normalize_ticker_to_asset_id("GBP/USD") == "gbpusd"


def test_normalize_ticker_to_asset_id_accepts_already_normalized():
    assert normalize_ticker_to_asset_id("gbpusd") == "gbpusd"


# --- watchlist: format_watchlist_list ---

def test_format_watchlist_list_empty():
    assert "порожній" in format_watchlist_list([])


def test_format_watchlist_list_includes_asset_label_and_source():
    rows = [{"asset_id": "xauusd", "source": "twelvedata", "label": "Золото (XAU/USD)"}]
    text = format_watchlist_list(rows)
    assert "xauusd" in text
    assert "Золото (XAU/USD)" in text
    assert "twelvedata" in text


# --- watchlist: validate_twelvedata_ticker ---

def test_validate_twelvedata_ticker_true_when_records_returned(monkeypatch):
    monkeypatch.setattr(
        telegram_commands, "TwelveDataAdapter",
        lambda api_key, ticker: type("A", (), {"collect": lambda self, limit: [object()]})(),
    )
    assert validate_twelvedata_ticker("GBP/USD", "key") is True


def test_validate_twelvedata_ticker_false_when_no_records(monkeypatch):
    monkeypatch.setattr(
        telegram_commands, "TwelveDataAdapter",
        lambda api_key, ticker: type("A", (), {"collect": lambda self, limit: []})(),
    )
    assert validate_twelvedata_ticker("NOTREAL", "key") is False


def test_validate_twelvedata_ticker_false_on_value_error(monkeypatch):
    def _raise(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(telegram_commands, "TwelveDataAdapter", _raise)
    assert validate_twelvedata_ticker("BAD", "key") is False


# --- watchlist: validate_binance_symbol ---

def test_validate_binance_symbol_true_when_records_returned(monkeypatch):
    monkeypatch.setattr(
        telegram_commands, "BinanceAdapter",
        lambda metric_id, symbol: type("A", (), {"collect": lambda self, limit: [object()]})(),
    )
    assert validate_binance_symbol("BNBUSDT") is True


def test_validate_binance_symbol_false_when_no_records(monkeypatch):
    monkeypatch.setattr(
        telegram_commands, "BinanceAdapter",
        lambda metric_id, symbol: type("A", (), {"collect": lambda self, limit: []})(),
    )
    assert validate_binance_symbol("FAKEUSDT") is False


def test_validate_binance_symbol_false_on_value_error(monkeypatch):
    def _raise(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(telegram_commands, "BinanceAdapter", _raise)
    assert validate_binance_symbol("BADUSDT") is False


# --- watchlist: normalize_ticker_to_asset_id (оновлено 2026-10-03) ---

def test_normalize_ticker_to_asset_id_strips_usdt_suffix():
    # 'BNB' і 'BNBUSDT' мають дати ОДНАКОВИЙ asset_id — незалежно від
    # того, яке джерело зрештою підтвердить тикер.
    assert normalize_ticker_to_asset_id("BNBUSDT") == "bnb"
    assert normalize_ticker_to_asset_id("BNB") == "bnb"


def test_normalize_ticker_to_asset_id_forex_pair_unaffected():
    # "USD" наприкінці форекс-пари не плутається з суфіксом "USDT".
    assert normalize_ticker_to_asset_id("GBP/USD") == "gbpusd"


# --- watchlist: handle_watchlist_add — пробує джерела по черзі ---

def test_handle_watchlist_add_requires_argument():
    assert "Вкажіть тикер" in handle_watchlist_add(conn=None, ticker_arg="", twelvedata_api_key="key")


def test_handle_watchlist_add_already_enabled(monkeypatch):
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: {"enabled": True})
    text = handle_watchlist_add(conn=None, ticker_arg="GBP/USD", twelvedata_api_key="key")
    assert "уже є" in text


def test_handle_watchlist_add_re_enables_disabled_asset(monkeypatch):
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: {"enabled": False})
    enabled_calls = []
    monkeypatch.setattr(
        telegram_commands, "set_enabled",
        lambda conn, asset_id, enabled: enabled_calls.append((asset_id, enabled)),
    )

    text = handle_watchlist_add(conn=None, ticker_arg="GBP/USD", twelvedata_api_key="key")

    assert enabled_calls == [("gbpusd", True)]
    assert "повернуто" in text


def test_handle_watchlist_add_uses_twelvedata_when_it_confirms(monkeypatch):
    # 2026-10-03: ЖОДНОГО вгадування за форматом тикера — Twelve Data
    # пробується першим на БУДЬ-ЯКОМУ тикері (навіть без "/", напр.
    # commodity-символи типу NATGAS), не лише на тих, що містять "/".
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: None)
    monkeypatch.setattr(telegram_commands, "validate_twelvedata_ticker", lambda ticker, api_key: True)
    monkeypatch.setattr(telegram_commands, "validate_gdelt_term", lambda term: True)
    monkeypatch.setattr(
        telegram_commands, "validate_binance_symbol",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Binance не мав пробуватись — Twelve Data вже підтвердив")),
    )
    added = []
    monkeypatch.setattr(telegram_commands, "add_asset", lambda conn, **kwargs: added.append(kwargs))

    text = handle_watchlist_add(conn=None, ticker_arg="NATGAS", twelvedata_api_key="key")

    assert len(added) == 1
    assert added[0]["asset_id"] == "natgas"
    assert added[0]["source"] == "twelvedata"
    assert added[0]["metric_id"] == "natgas_close"
    assert added[0]["ticker"] == "NATGAS"
    assert added[0]["search_term"] == '"NATGAS"'
    assert "Додано" in text
    assert "NATGAS" in text
    assert "Twelve Data" in text


def test_handle_watchlist_add_stock_gets_class_company_and_second_source(monkeypatch):
    """Живий кейс SKYD (2026-10-22): акцію додано без типу (у списку
    торгів — "товар"), без CIK і без назви компанії — тож ні звітності,
    ні новин по компанії. Тепер усе це визначається при додаванні."""
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "test agent test@example.com")
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: None)
    monkeypatch.setattr(telegram_commands, "validate_twelvedata_ticker", lambda ticker, api_key: True)
    monkeypatch.setattr(telegram_commands, "validate_gdelt_term", lambda term: True)
    from common import profile_lookup

    monkeypatch.setattr(profile_lookup, "lookup_instrument", lambda query, api_key=None: {
        "symbol": "SKYD", "instrument_name": "Skydance Corporation Class B Common Stock",
        "instrument_type": "Common Stock", "exchange": "NYSE", "country": "United States",
    })
    monkeypatch.setattr(
        profile_lookup, "lookup_company",
        lambda ticker, user_agent: {"cik": "0002041610", "title": "Skydance Corp"},
    )
    yahoo_checked = []
    monkeypatch.setattr(
        profile_lookup, "validate_yahoo_symbol",
        lambda asset_id, symbol: yahoo_checked.append(symbol) or True,
    )
    added = []
    monkeypatch.setattr(telegram_commands, "add_asset", lambda conn, **kwargs: added.append(kwargs))

    text = handle_watchlist_add(conn=None, ticker_arg="SKYD", twelvedata_api_key="key")

    row = added[0]
    profile = row["profile"]
    assert profile["asset_class"] == "stock"
    assert profile["cik"] == "0002041610"
    assert profile["company_name"] == "Skydance Corp"
    assert row["label"] == "Skydance (SKYD)"
    # GDELT — назва компанії, не тикер
    assert row["search_term"] == '"Skydance"'
    assert r"\bskydance\b" in profile["news_patterns"]
    assert yahoo_checked == ["SKYD"]
    assert [c["source"] for c in profile["price_chain"]] == ["twelvedata", "yahoo_chart"]
    assert profile["price_chain"][0]["metric_id"] == "skyd_close"
    assert "акція" in text


def test_handle_watchlist_add_unknown_type_is_not_guessed(monkeypatch):
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: None)
    monkeypatch.setattr(telegram_commands, "validate_twelvedata_ticker", lambda ticker, api_key: True)
    monkeypatch.setattr(telegram_commands, "validate_gdelt_term", lambda term: True)
    added = []
    monkeypatch.setattr(telegram_commands, "add_asset", lambda conn, **kwargs: added.append(kwargs))

    text = handle_watchlist_add(conn=None, ticker_arg="NATGAS", twelvedata_api_key="key")

    assert added[0]["profile"]["asset_class"] is None
    assert "тип не визначено" in text


def test_handle_watchlist_add_resolves_via_twelvedata_symbol_search_when_exact_fails(monkeypatch):
    # 2026-10-03, живий кейс "NATGAS": точний рядок дав 404 у Twelve
    # Data, але symbol_search знаходить правильний каталожний код (тут
    # "NG") — підтверджується ще раз живими даними перед додаванням.
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: None)

    validated_tickers = []

    def fake_validate(ticker, api_key):
        validated_tickers.append(ticker)
        return ticker == "NG"  # лише резолвлений символ підтверджується

    monkeypatch.setattr(telegram_commands, "validate_twelvedata_ticker", fake_validate)
    monkeypatch.setattr(telegram_commands, "validate_gdelt_term", lambda term: True)
    search_calls = []
    monkeypatch.setattr(
        telegram_commands, "search_twelvedata_symbol",
        lambda query, api_key: search_calls.append(query) or "NG",
    )
    monkeypatch.setattr(
        telegram_commands, "validate_binance_symbol",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Binance не мав пробуватись — Twelve Data вже підтвердив")),
    )
    added = []
    monkeypatch.setattr(telegram_commands, "add_asset", lambda conn, **kwargs: added.append(kwargs))

    text = handle_watchlist_add(conn=None, ticker_arg="NATGAS", twelvedata_api_key="key")

    assert validated_tickers == ["NATGAS", "NG"]
    assert search_calls == ["NATGAS"]
    assert len(added) == 1
    assert added[0]["ticker"] == "NG"
    assert added[0]["search_term"] == '"NG"'
    assert "Додано" in text
    assert "NG" in text


def test_handle_watchlist_add_symbol_search_candidate_must_also_validate(monkeypatch):
    # symbol_search сам лише довідниковий каталог — якщо знайдений
    # кандидат ТЕЖ не має живих даних, Twelve Data-шлях все одно
    # вважається невдалим (падає далі на Binance), не додає "порожній" актив.
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: None)
    monkeypatch.setattr(telegram_commands, "validate_twelvedata_ticker", lambda ticker, api_key: False)
    monkeypatch.setattr(telegram_commands, "search_twelvedata_symbol", lambda query, api_key: "NG")
    monkeypatch.setattr(telegram_commands, "validate_binance_symbol", lambda symbol: True)
    monkeypatch.setattr(telegram_commands, "validate_gdelt_term", lambda term: True)
    added = []
    monkeypatch.setattr(telegram_commands, "add_asset", lambda conn, **kwargs: added.append(kwargs))

    text = handle_watchlist_add(conn=None, ticker_arg="NATGAS", twelvedata_api_key="key")

    assert len(added) == 1
    assert added[0]["source"] == "binance"  # не twelvedata — кандидат не підтвердився
    assert "Додано" in text


def test_handle_watchlist_add_falls_back_to_binance_when_twelvedata_does_not_confirm(monkeypatch):
    # 2026-10-03, живий кейс BNB: Twelve Data не знає крипто — падає
    # далі на Binance, без жодної ручної підказки джерела від користувача.
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: None)
    monkeypatch.setattr(telegram_commands, "validate_twelvedata_ticker", lambda ticker, api_key: False)
    monkeypatch.setattr(telegram_commands, "search_twelvedata_symbol", lambda query, api_key: None)
    monkeypatch.setattr(telegram_commands, "validate_gdelt_term", lambda term: True)
    validate_calls = []
    monkeypatch.setattr(
        telegram_commands, "validate_binance_symbol",
        lambda symbol: validate_calls.append(symbol) or True,
    )
    added = []
    monkeypatch.setattr(telegram_commands, "add_asset", lambda conn, **kwargs: added.append(kwargs))

    text = handle_watchlist_add(conn=None, ticker_arg="BNB", twelvedata_api_key="key")

    assert validate_calls == ["BNBUSDT"]
    assert len(added) == 1
    assert added[0]["asset_id"] == "bnb"
    assert added[0]["source"] == "binance"
    assert added[0]["metric_id"] == "bnb_close"
    assert added[0]["ticker"] == "BNBUSDT"
    assert added[0]["label"] == "BNB/USDT"
    assert added[0]["search_term"] == '"BNB"'
    assert added[0]["profile"]["asset_class"] == "crypto"
    assert r"\bbnb\b" in added[0]["profile"]["news_patterns"]
    assert "Додано" in text
    assert "BNB/USDT" in text
    assert "Binance" in text


def test_handle_watchlist_add_skips_twelvedata_without_api_key_and_tries_binance(monkeypatch):
    # Відсутній TWELVEDATA_API_KEY більше НЕ блокує команду цілком —
    # мовчки пропускає спробу Twelve Data, пробує Binance.
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: None)
    monkeypatch.setattr(
        telegram_commands, "validate_twelvedata_ticker",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("не мало викликатись без ключа")),
    )
    monkeypatch.setattr(telegram_commands, "validate_binance_symbol", lambda symbol: True)
    monkeypatch.setattr(telegram_commands, "validate_gdelt_term", lambda term: True)
    added = []
    monkeypatch.setattr(telegram_commands, "add_asset", lambda conn, **kwargs: added.append(kwargs))

    text = handle_watchlist_add(conn=None, ticker_arg="BNB", twelvedata_api_key=None)

    assert len(added) == 1
    assert added[0]["source"] == "binance"
    assert "Додано" in text


def test_handle_watchlist_add_rejects_symbol_unconfirmed_by_any_source(monkeypatch):
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: None)
    monkeypatch.setattr(telegram_commands, "validate_twelvedata_ticker", lambda ticker, api_key: False)
    monkeypatch.setattr(telegram_commands, "search_twelvedata_symbol", lambda query, api_key: None)
    monkeypatch.setattr(telegram_commands, "validate_binance_symbol", lambda symbol: False)
    added = []
    monkeypatch.setattr(telegram_commands, "add_asset", lambda conn, **kwargs: added.append(kwargs))

    text = handle_watchlist_add(conn=None, ticker_arg="NOTREAL", twelvedata_api_key="key")

    assert added == []
    assert "Жодне з доступних джерел" in text
    assert "NOTREAL" in text


def test_handle_watchlist_add_accepts_symbol_with_usdt_suffix_already(monkeypatch):
    # Користувач може ввести і "BNB", і "BNBUSDT" — обидва дають той
    # самий asset_id/symbol, не "BNBUSDTUSDT".
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: None)
    monkeypatch.setattr(telegram_commands, "validate_twelvedata_ticker", lambda ticker, api_key: False)
    monkeypatch.setattr(telegram_commands, "search_twelvedata_symbol", lambda query, api_key: None)
    monkeypatch.setattr(telegram_commands, "validate_gdelt_term", lambda term: True)
    validate_calls = []
    monkeypatch.setattr(
        telegram_commands, "validate_binance_symbol",
        lambda symbol: validate_calls.append(symbol) or True,
    )
    monkeypatch.setattr(telegram_commands, "add_asset", lambda conn, **kwargs: None)

    handle_watchlist_add(conn=None, ticker_arg="BNBUSDT", twelvedata_api_key="key")

    assert validate_calls == ["BNBUSDT"]


# --- watchlist: _resolve_news_search_term (2026-10-04, живий кейс "BNB") ---

def test_resolve_news_search_term_accepts_primary_when_gdelt_confirms(monkeypatch):
    monkeypatch.setattr(telegram_commands, "validate_gdelt_term", lambda term: True)
    assert _resolve_news_search_term("EUR/USD") == ('"EUR/USD"', "validated")


def test_resolve_news_search_term_falls_back_to_widened(monkeypatch):
    monkeypatch.setattr(telegram_commands, "validate_gdelt_term", lambda term: term == "BNB crypto")
    assert _resolve_news_search_term("BNB", widened="BNB crypto") == ('"BNB crypto"', "validated")


def test_resolve_news_search_term_rejected_when_gdelt_refuses_both(monkeypatch):
    """GDELT ВІДМОВИВ (False) — остаточно: фраза не стане довшою від
    повтору, тож актив іде без новинного терміну."""
    monkeypatch.setattr(telegram_commands, "validate_gdelt_term", lambda term: False)
    assert _resolve_news_search_term("BNB", widened="BNB crypto") == (None, "rejected")


def test_resolve_news_search_term_no_widened_option_returns_rejected(monkeypatch):
    monkeypatch.setattr(telegram_commands, "validate_gdelt_term", lambda term: False)
    assert _resolve_news_search_term("NG") == (None, "rejected")


def test_resolve_news_search_term_pending_when_check_impossible(monkeypatch):
    """КОРІНЬ живого фідбеку 2026-10-06 ("не бачу в запиті активів,
    доданих через телеграм"): коли перевірку не вдалось провести
    (None — 429/мʼякий троттл), термін МУСИТЬ зберегтись як 'pending',
    а не зникнути. Інакше з баном GDELT кожен новий актив назавжди
    лишався б без новин, мовчки."""
    monkeypatch.setattr(telegram_commands, "validate_gdelt_term", lambda term: None)
    term, status = _resolve_news_search_term("BNB", widened="BNB crypto")
    assert status == "pending"
    # Кандидатом береться ШИРШИЙ варіант — у нього більше шансів
    # пройти, бо коротка фраза і є головною причиною відмов GDELT.
    assert term == '"BNB crypto"'


def test_resolve_news_search_term_prefers_a_confirmed_term_over_pending(monkeypatch):
    """Якщо основний термін підтверджено, недоступність перевірки для
    ширшого варіанту вже не має значення."""
    monkeypatch.setattr(
        telegram_commands, "validate_gdelt_term",
        lambda term: True if term == "gold price" else None,
    )
    assert _resolve_news_search_term("gold price", widened="gold") == ('"gold price"', "validated")


def test_handle_watchlist_add_still_adds_asset_when_gdelt_rejects_all_terms(monkeypatch):
    # 2026-10-04, живий кейс "BNB": GDELT відхиляє і "BNB", і "BNB
    # crypto" — АКТИВ УСЕ ОДНО додається (ціна збирається), просто без
    # участі в GDELT-запиті (search_term=None, не падає й не блокує
    # команду).
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: None)
    monkeypatch.setattr(telegram_commands, "validate_twelvedata_ticker", lambda ticker, api_key: False)
    monkeypatch.setattr(telegram_commands, "search_twelvedata_symbol", lambda query, api_key: None)
    monkeypatch.setattr(telegram_commands, "validate_binance_symbol", lambda symbol: True)
    monkeypatch.setattr(telegram_commands, "validate_gdelt_term", lambda term: False)
    added = []
    monkeypatch.setattr(telegram_commands, "add_asset", lambda conn, **kwargs: added.append(kwargs))

    text = handle_watchlist_add(conn=None, ticker_arg="BNB", twelvedata_api_key="key")

    assert len(added) == 1
    assert added[0]["search_term"] is None
    assert "Додано" in text
    assert "без новинного пошуку" in text


# --- watchlist: handle_watchlist_remove ---

def test_handle_watchlist_remove_requires_argument():
    assert "Вкажіть актив" in handle_watchlist_remove(conn=None, asset_arg="")


def test_handle_watchlist_remove_not_found(monkeypatch):
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: None)
    text = handle_watchlist_remove(conn=None, asset_arg="gbpusd")
    assert "немає" in text


def test_handle_watchlist_remove_deletes_asset_and_derived_analyses(monkeypatch):
    """2026-10-22 (пункт 2 плану): видалення = видалення, не
    `enabled = false` — актив зникає з чек-листа й аналізів."""
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: {"enabled": True, "source": "fred"})
    removed_calls = []
    monkeypatch.setattr(
        telegram_commands, "remove_asset",
        lambda conn, asset_id: removed_calls.append(asset_id) or {
            "asset_checklists": 1, "fundamental_analysis": 3, "news_synthesis": 2,
        },
    )
    monkeypatch.setattr(
        telegram_commands, "set_enabled",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("вимикати більше не можна — лише видаляти")),
    )

    text = handle_watchlist_remove(conn=None, asset_arg="XAU/USD")

    assert removed_calls == ["xauusd"]
    assert "видалено" in text
    assert "чек-лист видалено" in text
    assert "— 5" in text  # 3 + 2 похідних аналізи
    assert "Сирі ціни" in text


def test_handle_watchlist_remove_also_deletes_previously_disabled_asset(monkeypatch):
    # Рядки, вимкнені до 2026-10-22, теж видаляються (а не "уже вимкнений").
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: {"enabled": False})
    monkeypatch.setattr(telegram_commands, "remove_asset", lambda conn, asset_id: {})
    text = handle_watchlist_remove(conn=None, asset_arg="gbpusd")
    assert "видалено" in text


# --- watchlist: process_update dispatch ---

def test_process_update_watchlist_list_dispatches(monkeypatch):
    sent = []
    # 2026-10-06: /watchlist_list показує СТАН списку (ціна+дата+статус
    # новинного терміну), тож читає fetch_watchlist_health(), не
    # fetch_watchlist().
    monkeypatch.setattr(
        telegram_commands, "fetch_watchlist_health",
        lambda conn: [{
            "asset_id": "btc", "source": "binance", "label": "BTC/USDT",
            "metric_id": "btc_close", "enabled": True, "added_via": "seed",
            "search_term": "Bitcoin", "search_term_status": "validated",
            "price": Decimal("65000"), "observed_at": date.today(),
        }],
    )
    monkeypatch.setattr(telegram_commands, "send_telegram_message", lambda token, chat_id, text, **k: sent.append(text))

    process_update(_message("/watchlist_list"), configured_chat_id="123456", token="T", conn=object())

    assert len(sent) == 1
    assert "btc" in sent[0]
    assert "у новинному запиті" in sent[0]


def test_process_update_watchlist_add_dispatches_with_parsed_args(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        telegram_commands, "handle_watchlist_add",
        lambda conn, ticker_arg, twelvedata_api_key: captured.update(ticker_arg=ticker_arg, key=twelvedata_api_key) or "ok",
    )
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: None)
    sent = []
    monkeypatch.setattr(telegram_commands, "send_telegram_message", lambda token, chat_id, text, **k: sent.append(text))

    process_update(
        _message("/watchlist_add GBP/USD"), configured_chat_id="123456", token="T",
        conn=object(), twelvedata_api_key="key123",
    )

    assert captured == {"ticker_arg": "GBP/USD", "key": "key123"}
    assert sent == ["ok"]


def _watchlist_add_with_states(monkeypatch, before, after):
    """`find_asset` до й після додавання → чи запустився онбординг
    (2026-10-22: дані активу, потім чек-лист)."""
    states = iter([before, after])
    monkeypatch.setattr(telegram_commands, "find_asset", lambda conn, asset_id: next(states))
    monkeypatch.setattr(telegram_commands, "handle_watchlist_add", lambda conn, ticker_arg, twelvedata_api_key: "ok")
    monkeypatch.setattr(telegram_commands, "send_telegram_message", lambda *a, **k: None)
    launched = []
    monkeypatch.setattr(telegram_commands, "launch_onboarding", launched.append)
    process_update(_message("/watchlist_add BNB"), configured_chat_id="123456", token="T", conn=object())
    return launched


def test_watchlist_add_launches_checklist_for_new_asset(monkeypatch):
    """Щойно доданий актив одразу отримує свій чек-лист окремим
    повідомленням (2026-10-20) — а не лише в аналізі наступного дня."""
    launched = _watchlist_add_with_states(monkeypatch, None, {"asset_id": "bnb", "enabled": True})
    assert launched == ["bnb"]


def test_watchlist_add_launches_checklist_for_reenabled_asset(monkeypatch):
    launched = _watchlist_add_with_states(
        monkeypatch, {"asset_id": "bnb", "enabled": False}, {"asset_id": "bnb", "enabled": True},
    )
    assert launched == ["bnb"]


def test_watchlist_add_does_not_relaunch_for_existing_or_failed(monkeypatch):
    """Актив уже був увімкнений — нічого нового; не додався — чек-листу
    нема для чого складатись (кожен запуск — платний виклик ШІ)."""
    assert _watchlist_add_with_states(
        monkeypatch, {"asset_id": "bnb", "enabled": True}, {"asset_id": "bnb", "enabled": True},
    ) == []
    assert _watchlist_add_with_states(monkeypatch, None, None) == []


def test_process_update_watchlist_remove_dispatches_with_parsed_args(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        telegram_commands, "handle_watchlist_remove",
        lambda conn, asset_arg: captured.update(asset_arg=asset_arg) or "ok",
    )
    sent = []
    monkeypatch.setattr(telegram_commands, "send_telegram_message", lambda token, chat_id, text, **k: sent.append(text))

    process_update(_message("/watchlist_remove gbpusd"), configured_chat_id="123456", token="T", conn=object())

    assert captured == {"asset_arg": "gbpusd"}
    assert sent == ["ok"]


# --- offset ---

class _FakeCursor:
    def __init__(self, fetchone_result, log):
        self._fetchone_result = fetchone_result
        self._log = log

    def execute(self, sql, params=None):
        self._log.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._fetchone_result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, fetchone_result=None):
        self.log = []
        self.committed = False
        self._fetchone_result = fetchone_result

    def cursor(self):
        return _FakeCursor(self._fetchone_result, self.log)

    def commit(self):
        self.committed = True


def test_get_offset_defaults_to_zero_when_no_row():
    conn = _FakeConn(fetchone_result=None)
    assert get_offset(conn) == 0


def test_get_offset_returns_last_plus_one():
    conn = _FakeConn(fetchone_result=(41,))
    assert get_offset(conn) == 42


def test_save_offset_upserts_and_commits():
    conn = _FakeConn()
    save_offset(conn, 42)

    assert conn.committed is True
    sql, params = conn.log[0]
    assert "INSERT INTO telegram_command_offset" in sql
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert params == (42,)


# --- онбординг нового активу (2026-10-22, живий кейс SKYD) ---

def test_onboarding_collects_company_data_before_checklist_for_stock():
    """Чек-лист SKYD складався, коли даних компанії в базі ще не було.
    Тепер для акції спершу ціни, звітність SEC і тексти звітів — і
    лише потім чек-лист."""
    from onboard_asset import plan_steps

    steps = [name for name, _ in plan_steps({"asset_id": "skyd", "asset_class": "stock", "ticker": "SKYD"},
                                            dry_run=False, notify=True)]
    assert steps == ["ціни", "звітність SEC", "події, інсайдери, аналітики", "тексти 10-K/10-Q", "чек-лист"]


def test_onboarding_non_stock_skips_company_steps():
    from onboard_asset import plan_steps

    steps = [name for name, _ in plan_steps({"asset_id": "bnb", "asset_class": "crypto", "ticker": "BNBUSDT"},
                                            dry_run=False, notify=True)]
    assert steps == ["ціни", "чек-лист"]


def test_onboarding_dry_run_never_calls_llm():
    """Дев: токени не витрачаються — тези зі звіту не витягуються,
    чек-лист лише друкує промпт."""
    from onboard_asset import plan_steps

    commands = dict(plan_steps({"asset_id": "skyd", "asset_class": "stock", "ticker": "SKYD"},
                               dry_run=True, notify=True))
    assert "--collect-only" in commands["тексти 10-K/10-Q"]
    assert "--dry-run" in commands["чек-лист"]
    assert "--notify" not in commands["чек-лист"]


# --- сесійний бриф (2026-10-22, пункт 5 плану сесії) ---

def test_session_brief_order_trading_list_then_synthesis_then_send():
    """Бриф мусить показувати СВІЖИЙ список торгів цієї сесії й
    надсилатись одразу після синтезу, а не окремою джобою за годину."""
    from session_brief import plan_steps

    names = [name for name, _ in plan_steps("us", dry_run=False)]
    assert names == ["список для торгів", "синтез сесії", "надсилання"]


def test_session_brief_dry_run_never_calls_llm_or_telegram():
    from session_brief import plan_steps

    for _name, command in plan_steps("asia", dry_run=True):
        assert "--dry-run" in command
