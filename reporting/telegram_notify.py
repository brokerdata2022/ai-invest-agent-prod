#!/usr/bin/env python3
"""
Мінімальне сповіщення в Telegram про останнє зібране значення показника.

Це НЕ "короткий регулярний звіт" з reporting/CLAUDE.md (той з'явиться
у Фазі 4, коли буде що агрегувати з analysis/) — це технічна перевірка
критерію завершення Фази 0: дані реально дійшли від збору до
користувача. Тому тут тільки форматування вже збереженого факту,
жодної інтерпретації "добре це чи погано".

Використання:
    python telegram_notify.py --metric cpi
"""

import argparse
import logging
import sys

from dotenv import load_dotenv

# _common додає data-ingestion у sys.path (дефіс у назві теки —
# не валідне ім'я Python-пакета), тому імпортується ПЕРШИМ.
from _common import bold, escape_html, resolve_telegram_credentials  # noqa: E402
from common.db import get_connection, fetch_latest  # noqa: E402
from telegram_client import send_telegram_message  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Людські підписи для показників — суто для форматування повідомлення,
# не аналітика.
METRIC_LABELS = {
    "cpi": "CPI (інфляція, США)",
    "fed_funds_rate": "Fed Funds Rate",
    "unemployment_rate": "Рівень безробіття (США)",
    "core_cpi": "Core CPI (без їжі/енергії, США)",
    "pce_price_index": "PCE Price Index (орієнтир ФРС)",
    "nonfarm_payrolls": "Non-Farm Payrolls (США)",
    "treasury_10y": "10Y Treasury Yield (США)",
    "treasury_2y": "2Y Treasury Yield (США)",
    "initial_jobless_claims": "Initial Jobless Claims (США)",
    "real_gdp": "Real GDP (США)",
    "retail_sales": "Retail Sales (США)",
    "housing_starts": "Housing Starts (США)",
    "mortgage_rate_30y": "30Y Fixed Mortgage Rate (США)",
    "usdjpy_fx_rate": "USD/JPY (курс, для carry trade)",
    "eurozone_hicp": "HICP (інфляція, єврозона)",
    "eurozone_deposit_rate": "Deposit Facility Rate (єврозона)",
    "eurozone_unemployment_rate": "Рівень безробіття (єврозона)",
    "japan_policy_rate": "Policy Rate (Uncollateralized O/N Call Rate, Японія)",
    "japan_cpi": "CPI (інфляція, Японія)",
    "btc_close": "BTC/USDT (ціна закриття)",
    "btc_volume": "BTC/USDT (обсяг торгів)",
    "btc_market_cap": "BTC (market cap)",
    "eth_close": "ETH/USDT (ціна закриття)",
    "eth_volume": "ETH/USDT (обсяг торгів)",
    "eth_market_cap": "ETH (market cap)",
    "sol_close": "SOL/USDT (ціна закриття)",
    "sol_volume": "SOL/USDT (обсяг торгів)",
    "sol_market_cap": "SOL (market cap)",
    "xagusd_close": "XAG/USD (срібло, проксі через kinesis-silver)",
}

# metric_id → джерело, для автовизначення --source, якщо не задано явно.
# Дублює розподіл із data-ingestion/run_collect.py:ADAPTERS — тримаємо
# тут окремо (без прямого імпорту), бо reporting/ і data-ingestion/ —
# незалежні контейнери/деплойменти (див. reporting/CLAUDE.md).
_METRIC_SOURCE = {
    "cpi": "fred", "fed_funds_rate": "fred", "unemployment_rate": "fred",
    "core_cpi": "fred", "pce_price_index": "fred", "nonfarm_payrolls": "fred",
    "treasury_10y": "fred", "treasury_2y": "fred", "initial_jobless_claims": "fred",
    "real_gdp": "fred", "retail_sales": "fred", "housing_starts": "fred",
    "mortgage_rate_30y": "fred", "usdjpy_fx_rate": "fred",
    "eurozone_hicp": "ecb", "eurozone_deposit_rate": "ecb",
    "eurozone_unemployment_rate": "ecb",
    "japan_policy_rate": "boj",
    "japan_cpi": "estat",
    "btc_close": "binance", "btc_volume": "binance", "btc_market_cap": "coingecko",
    "eth_close": "binance", "eth_volume": "binance", "eth_market_cap": "coingecko",
    "sol_close": "binance", "sol_volume": "binance", "sol_market_cap": "coingecko",
    "xagusd_close": "coingecko",
}


def format_message(row: dict, metric_id: str) -> str:
    label = METRIC_LABELS.get(metric_id, metric_id)
    return (
        f"{bold('📊 ' + label)}\n"
        f"Значення: {bold(row['value'])}\n"
        f"За період: {escape_html(row['observed_at'])}\n"
        f"Джерело: {escape_html(row['source'])} (ревізія {escape_html(row['revision'])})\n"
        f"Зібрано: {escape_html(row['fetched_at'])}"
    )


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metric", required=True, help="metric_id, напр. cpi")
    parser.add_argument(
        "--source", default=None,
        help="за замовчуванням визначається автоматично за --metric",
    )
    args = parser.parse_args()

    source = args.source or _METRIC_SOURCE.get(args.metric)
    if source is None:
        logger.error(
            "Невідомий metric_id %r і --source не задано явно. "
            "Додайте metric_id у _METRIC_SOURCE або вкажіть --source.",
            args.metric,
        )
        sys.exit(1)

    token, chat_id = resolve_telegram_credentials()

    conn = get_connection()
    try:
        row = fetch_latest(conn, source=source, metric_id=args.metric)
    finally:
        conn.close()

    if row is None:
        logger.warning(
            "Немає даних для %s/%s — спершу запустіть "
            "data-ingestion/run_collect.py --metric %s",
            source, args.metric, args.metric,
        )
        sys.exit(1)

    text = format_message(row, args.metric)
    send_telegram_message(token, chat_id, text, parse_mode="HTML")
    logger.info("Надіслано в Telegram: %s", text.splitlines()[0])


if __name__ == "__main__":
    main()
