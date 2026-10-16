"""
ЄДИНИЙ конфіг агента (`config.py` у корені) — одне джерело істини
для всіх порогів (рішення користувача 2026-10-04: "конфіг один на весь
агент"; до того було по конфігу на модуль).

Тести стежать саме за цим: щоб модулі брали значення з `config.py`, а
не мали власних копій. Інакше правка в продакшені тихо не подіяла б —
рівно той клас проблеми, через який конфіг і попросили.
"""

import re
from pathlib import Path

import pytest

import config

_ANALYSIS_DIR = Path(__file__).resolve().parent.parent

# Назва константи → модулі, які її СПОЖИВАЮТЬ (і не мають визначати).
CONSUMERS = {
    "ANOMALY_SIGMA_MULTIPLIER": ["news_analysis/prices.py"],
    "ANOMALY_MIN_HISTORY": ["news_analysis/prices.py"],
    "ANOMALY_HISTORY_LOOKBACK": ["news_analysis/prices.py"],
    "SIMILARITY_THRESHOLD": ["news_analysis/aggregate.py"],
    "CROSS_RUN_SIMILARITY_THRESHOLD": ["news_analysis/_consolidated_db.py"],
    "MIN_NORMALIZED_LENGTH_FOR_MATCHING": ["news_analysis/aggregate.py"],
    # Скринінг акцій — перенесений 2026-10-05 (при першому зливанні
    # лишився в модулях, тобто вимога "один конфіг" була виконана
    # наполовину).
    "MIN_PRICE": ["screening/tier_a.py"],
    "MIN_MARKET_CAP": ["screening/tier_a.py"],
    "MIN_AVG_DOLLAR_VOLUME": ["screening/tier_a.py"],
    "MIN_REVENUE_YOY": ["screening/tier_b.py"],
    "MIN_EPS_YOY": ["screening/tier_b.py"],
    "MAX_LIABILITIES_TO_ASSETS": ["screening/tier_b.py"],
    "MIN_PE": ["screening/tier_c.py"],
    "MAX_PE": ["screening/tier_c.py"],
    "MAX_PEG": ["screening/tier_c.py"],
    "WEIGHT_REVENUE_GROWTH": ["screening/composite_score.py"],
    # Пошук новачків (2026-10-05, розділ 7 конфіга).
    "DISCOVERY_CRYPTO_MIN_VOLUME_24H": ["discovery/crypto_movers.py"],
    "DISCOVERY_CRYPTO_MIN_MOVE_PCT": ["discovery/crypto_movers.py"],
    "DISCOVERY_CRYPTO_SATURATION_PCT": ["discovery/crypto_movers.py"],
    "DISCOVERY_STOCK_MIN_MOVE_PCT": ["discovery/stock_movers.py"],
    "DISCOVERY_STOCK_SATURATION_PCT": ["discovery/stock_movers.py"],
    "DISCOVERY_STOCK_SHORT_DAYS": ["discovery/stock_movers.py"],
    "DISCOVERY_STOCK_LONG_DAYS": ["discovery/stock_movers.py"],
    "DISCOVERY_STOCK_MIN_POINTS": ["discovery/stock_movers.py", "discovery/news_tickers.py"],
    "DISCOVERY_STOCK_MAX_DAILY_MOVE_PCT": ["discovery/stock_movers.py"],
    "DISCOVERY_NEWS_STREAMS": ["discovery/run_discovery.py"],
    "DISCOVERY_VOLUME_SPIKE_REFERENCE": ["discovery/crypto_movers.py", "discovery/stock_movers.py"],
    "DISCOVERY_W_MOVE": ["discovery/crypto_movers.py", "discovery/stock_movers.py"],
    "DISCOVERY_W_VOLUME": ["discovery/crypto_movers.py", "discovery/stock_movers.py"],
    "DISCOVERY_W_NEWS": ["discovery/crypto_movers.py", "discovery/stock_movers.py"],
    "DISCOVERY_NEWS_SOURCE_REFERENCE": ["discovery/crypto_movers.py", "discovery/stock_movers.py"],
    "DISCOVERY_NEWS_MAX_VERIFY": ["discovery/news_tickers.py"],
    "DISCOVERY_MIN_SCORE": ["discovery/run_discovery.py"],
    "DISCOVERY_MAX_ITEMS": ["discovery/run_discovery.py"],
    "DISCOVERY_MAX_PER_DETECTOR": ["discovery/run_discovery.py"],
    # Прогнозування (2026-10-05/06, розділ 10 конфіга).
    "FORECAST_MEANINGFUL_EDGE_PCT": ["forecasting/method_selector.py"],
    "FORECAST_FLAT_SERIES_POINTS": ["forecasting/method_selector.py"],
    "FORECAST_HISTORY_POINTS": ["forecasting/forecast_metric.py", "forecasting/backtest_llm.py"],
    "FORECAST_TREND_BASELINE_POINTS": ["forecasting/backtest_llm.py", "forecasting/backtest.py"],
    "FORECAST_DELTA_WINDOWS": ["forecasting/llm_forecast.py"],
    "FORECAST_PLAUSIBILITY_FACTOR": ["forecasting/llm_forecast.py"],
    # Чинники фундаментального аналізу (2026-10-04).
    "ASSET_FACTORS": ["fundamental/factors.py"],
    "ASSET_FACTOR_GAPS": ["fundamental/factors.py"],
    "DEFAULT_ASSET_FACTORS": ["fundamental/factors.py"],
}


@pytest.mark.parametrize("name", sorted(CONSUMERS))
def test_config_defines_every_tunable(name):
    assert hasattr(config, name), f"{name} зник із config.py"


@pytest.mark.parametrize("name,modules", sorted(CONSUMERS.items()))
def test_consumers_do_not_hardcode_value(name, modules):
    """Присвоєння ЛІТЕРАЛУ в модулі-споживачі означало б другу копію
    значення — і правка в config.py не подіяла б."""
    literal = re.compile(rf"^{name}\s*=\s*[0-9.]+\s*$", re.MULTILINE)
    for module in modules:
        text = (_ANALYSIS_DIR / module).read_text()
        assert not literal.search(text), (
            f"{module}: {name} присвоєно літералом — має братись із config.py"
        )


def test_prices_uses_config_values():
    """Не лише імпорт, а фактична рівність — щоб перейменування чи
    локальний перекрив не пройшли непоміченими."""
    from news_analysis import prices

    assert prices.ANOMALY_SIGMA_MULTIPLIER == config.ANOMALY_SIGMA_MULTIPLIER
    assert prices.ANOMALY_MIN_HISTORY == config.ANOMALY_MIN_HISTORY
    assert prices.ANOMALY_HISTORY_LOOKBACK == config.ANOMALY_HISTORY_LOOKBACK


def test_aggregate_uses_config_values():
    from news_analysis import aggregate

    assert aggregate.DEFAULT_SIMILARITY_THRESHOLD == config.SIMILARITY_THRESHOLD
    assert (
        aggregate.MIN_NORMALIZED_LENGTH_FOR_MATCHING
        == config.MIN_NORMALIZED_LENGTH_FOR_MATCHING
    )


def test_sigma_multiplier_is_sane():
    """0 зробив би аномальним будь-який рух, відʼємне — безглузде."""
    assert config.ANOMALY_SIGMA_MULTIPLIER > 0


def test_similarity_thresholds_in_unit_range():
    for value in (config.SIMILARITY_THRESHOLD, config.CROSS_RUN_SIMILARITY_THRESHOLD):
        assert 0 < value <= 1


def test_min_history_above_one():
    """stdev з однієї точки не існує — порог нижче 2 зламав би
    розрахунок волатильності."""
    assert config.ANOMALY_MIN_HISTORY >= 2


def test_lookback_not_smaller_than_min_history():
    """Інакше тягнули б менше точок, ніж вимагаємо для розрахунку."""
    assert config.ANOMALY_HISTORY_LOOKBACK >= config.ANOMALY_MIN_HISTORY


def test_composite_weights_sum_to_one():
    """Ваги ранжування скринінгу мусять давати 1 — інакше скор виходить
    за очікувану шкалу й пороги перестають читатись."""
    total = (
        config.WEIGHT_REVENUE_GROWTH + config.WEIGHT_EPS_GROWTH
        + config.WEIGHT_NEG_PE + config.WEIGHT_AVG_DOLLAR_VOLUME
        + config.WEIGHT_MOMENTUM
    )
    assert total == 1


def test_fundamentals_stay_majority_of_composite_score():
    """Моментум додано 2026-10-13, бо 85% ваги на КВАРТАЛЬНИХ даних
    тримали скринінг нерухомим 13 днів підряд. Але це лишається
    ФУНДАМЕНТАЛЬНИЙ скринер — ціна переупорядковує всередині
    відібраного пулу, а не визначає відбір. Якщо цінова частка колись
    перевищить фундаментальну, це вже інший інструмент, і зміна має
    бути свідомою, а не наслідком правки одного числа."""
    fundamental = (
        config.WEIGHT_REVENUE_GROWTH + config.WEIGHT_EPS_GROWTH + config.WEIGHT_NEG_PE
    )
    price_based = config.WEIGHT_AVG_DOLLAR_VOLUME + config.WEIGHT_MOMENTUM
    assert fundamental > price_based


def test_pe_range_is_ordered():
    assert config.MIN_PE < config.MAX_PE


def test_discovery_weights_sum_to_one():
    """Та сама вимога, що composite-скор скринінгу: ваги сигналів
    новачка мусять давати 1, інакше "сила 0..1" перестає означати те,
    що написано, і DISCOVERY_MIN_SCORE калібрується в нікуди."""
    total = config.DISCOVERY_W_MOVE + config.DISCOVERY_W_VOLUME + config.DISCOVERY_W_NEWS
    assert total == 1


def test_discovery_min_score_is_reachable_by_move_alone():
    """Головний урок "0 рядків за весь час": санітарний поріг не має
    права відсікати кандидата, у якого є лише рух ціни — обсягу й
    новин по новачку зазвичай немає."""
    min_signal = config.DISCOVERY_W_MOVE * (
        config.DISCOVERY_CRYPTO_MIN_MOVE_PCT / config.DISCOVERY_CRYPTO_SATURATION_PCT
    )
    assert min_signal >= config.DISCOVERY_MIN_SCORE


def test_discovery_thresholds_below_screening_ones():
    """Список СПОСТЕРЕЖЕННЯ мусить бути ширшим за список ТОРГІВ —
    інакше новачок не має шансу зʼявитись (саме це й сталось зі
    старою реалізацією)."""
    assert config.DISCOVERY_CRYPTO_MIN_VOLUME_24H < config.MIN_QUOTE_VOLUME_24H
    assert config.DISCOVERY_MIN_SCORE < config.MIN_SCORE


def test_discovery_daily_gate_above_entry_threshold():
    """Гейт корпоративних дій мусить бути ВИЩЕ за поріг входу, інакше
    він відкидав би саме ті рухи, які детектор і шукає."""
    assert config.DISCOVERY_STOCK_MAX_DAILY_MOVE_PCT > config.DISCOVERY_STOCK_MIN_MOVE_PCT


def test_discovery_news_streams_exclude_watchlist():
    """У потоці `watchlist` за визначенням наші ж активи — новачків
    там бути не може."""
    assert "watchlist" not in config.DISCOVERY_NEWS_STREAMS
    assert len(config.DISCOVERY_NEWS_STREAMS) >= 2


def test_discovery_stock_windows_ordered():
    assert config.DISCOVERY_STOCK_SHORT_DAYS < config.DISCOVERY_STOCK_LONG_DAYS
    assert config.DISCOVERY_STOCK_LOOKBACK_DAYS > config.DISCOVERY_STOCK_LONG_DAYS


def test_single_config_has_no_module_level_siblings():
    """Прямий захист вимоги "конфіг один на весь агент": модульних
    config.py більше бути не повинно."""
    from pathlib import Path

    analysis_dir = Path(__file__).resolve().parent.parent
    strays = sorted(p.relative_to(analysis_dir) for p in analysis_dir.glob("*/config.py"))
    assert not strays, f"зʼявились модульні конфіги: {strays}"


# ============================================================
# Прибирання похідних — захист правил 5 і 6
# ============================================================

# Таблиці, видалення яких тихо зламало б агента або порушило критичні
# правила. Тест існує, бо `DERIVED_RETENTION_DAYS` редагує людина, а
# ціна помилки тут асиметрична: зайве вікно нічого не ламає, зайва
# таблиця — ламає назавжди.
PROTECTED_FROM_PRUNING = (
    # Правило 6: сирі зібрані дані append-only назавжди (лише стиснення)
    "raw_observations",
    # Без вердиктів бектесту method_selector молча падає на naive по всіх показниках
    "backtest_verdicts",
    # Вимірювана історія ЯКОСТІ агента — те єдине, чим перевіряються ваги
    "metric_forecasts",
    "decision_outcomes",
    # Налаштовано людиною через Telegram, у коді цього немає
    "watchlist_assets",
    "sources",
    "release_log",
    "earnings_calendar",
)


def test_pruning_never_touches_protected_tables():
    import config as cfg

    for table in PROTECTED_FROM_PRUNING:
        assert table not in cfg.DERIVED_RETENTION_DAYS, (
            f"{table} не можна прибирати за розкладом — див. коментар "
            f"PROTECTED_FROM_PRUNING"
        )


def test_llm_call_referencing_tables_has_no_duplicates():
    """Хибно КОРОТКИЙ перелік видалив би промпт під живим висновком і
    тихо порушив правило 5; дублікати ж роздувають UNION ALL."""
    import config as cfg

    tables = cfg.LLM_CALL_REFERENCING_TABLES
    assert len(tables) == len(set(tables))


def test_retention_windows_are_positive_integers():
    import config as cfg

    for table, days in cfg.DERIVED_RETENTION_DAYS.items():
        assert isinstance(days, int) and days > 0, f"{table}: {days!r}"
    assert cfg.LLM_CALL_LOG_RETENTION_DAYS > 0


def test_output_health_checks_are_well_formed():
    """Перевірка описана ДАНИМИ, тож структуру стереже тест, а не
    падіння джоби о 22:30."""
    import config as cfg

    required_keys = {
        "name", "table", "time_column", "window_hours", "min_rows",
        "vary", "min_distinct", "required", "max_null_share",
    }
    names = set()
    for check in cfg.OUTPUT_HEALTH_CHECKS:
        assert required_keys <= set(check), check.get("name")
        assert check["name"] not in names, f"дубль перевірки {check['name']}"
        names.add(check["name"])
        assert 0.0 <= check["max_null_share"] <= 1.0
        assert isinstance(check["vary"], tuple)
        assert isinstance(check["required"], tuple)


# ============================================================
# Компресія raw_observations — НЕ вмикати без виконаних умов
# ============================================================


def test_schema_does_not_enable_compression():
    """Регресія на живий збій у продакшені 2026-10-16.

    Компресія, увімкнена 15.10, стиснула 1205 із 1211 чанків — і через
    9 годин ядро почало вбивати бекенди Postgres (signal 9) на
    ПЛАНУВАННІ запиту по 512 metric_id: `v_current_values` це
    `DISTINCT ON` без межі по даті, тож торкається всіх чанків, а
    кожен стиснутий додає власні вузли розпакування (зміряно: 40
    metric_id -> 8515 вузлів плану, 4.2 с планування).

    Умови, за яких компресію можна вмикати знову, перелічені в
    `db/schema.sql`. Цей тест падає, якщо хтось (включно зі мною)
    увімкне її, не виконавши їх."""
    from pathlib import Path

    schema = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
    text = schema.read_text(encoding="utf-8")

    assert "add_compression_policy" not in text, (
        "Компресію відкочено 2026-10-16 після OOM у продакшені — "
        "умови повторного ввімкнення див. db/schema.sql"
    )
    assert "timescaledb.compress = true" not in text
    assert "timescaledb.compress," not in text
