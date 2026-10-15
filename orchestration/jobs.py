"""
Реєстр ЩО виконати для кожної джоби, названої в schedule.py. Тільки
виклик існуючих скриптів/функцій — жодної бізнес-логіки тут (critical
rule 1 кореневого CLAUDE.md, orchestration/CLAUDE.md).

Два типи джоб:
- "subprocess": [sys.executable, "<шлях скрипта>", ...args] — той самий
  скрипт, що можна запустити руками через
  `docker compose exec app python <шлях> ...`.
- "callable": пряма Python-функція (для коду без CLI, напр. prices.py)
  — виконується в тому самому процесі/образі/.env, що scheduler.
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)


def _py(*args: str) -> list:
    return [sys.executable, *args]


def _crypto_prices() -> None:
    """Best-effort перебір Binance+CoinGecko метрик (BTC/ETH/SOL +
    xagusd) — той самий підхід, що data-ingestion/collect_all.py: одна
    метрика не валить решту."""
    sys.path.insert(0, str(REPO_ROOT / "data-ingestion"))
    from common.db import get_connection, insert_observations  # noqa: E402
    from crypto.binance_adapter import BinanceAdapter, METRICS as BINANCE_METRICS  # noqa: E402
    from crypto.coingecko_adapter import CoinGeckoAdapter, METRICS as COINGECKO_METRICS  # noqa: E402

    conn = get_connection()
    try:
        for adapter_class, metrics in (
            (BinanceAdapter, BINANCE_METRICS),
            (CoinGeckoAdapter, COINGECKO_METRICS),
        ):
            for metric_id in sorted(metrics):
                try:
                    records = adapter_class(metric_id=metric_id).collect(limit=5)
                    if records:
                        inserted = insert_observations(conn, records)
                        logger.info("%s: %d записів (%d нових/змінених)", metric_id, len(records), inserted)
                except Exception:
                    logger.error("%s: провалилось", metric_id, exc_info=True)
                    conn.rollback()
    finally:
        conn.close()


def _crypto_derivatives_collect() -> None:
    """Щоденний bulk-знімок 3 бірж ф'ючерсів (обсяг/funding/OI, де
    доступно bulk) — для крипто-скринінгу лонг/шорт/спостереження
    (analysis/crypto_screening/, docs/archive/plan-journal-2026-10-11.md Фаза 4, крок 4.5). Той самий
    підхід, що _crypto_prices(): одна біржа не валить решту.

    Критично для самої скринінг-логіки (не просто "збір заради збору"):
    `screen_long`/`screen_short_or_watch` (crypto_screening/) рахують
    зміну OI/обсягу за кілька днів — без цієї джоби raw_observations
    ніколи не накопичить історію, скільки не чекай (docs/decisions.md,
    2026-09-27, "крок 4.5")."""
    sys.path.insert(0, str(REPO_ROOT / "data-ingestion"))
    from common.db import get_connection, insert_observations  # noqa: E402
    from crypto.binance_futures_adapter import BinanceFuturesAdapter  # noqa: E402
    from crypto.bybit_futures_adapter import BybitFuturesAdapter  # noqa: E402
    from crypto.okx_futures_adapter import OkxFuturesAdapter  # noqa: E402

    conn = get_connection()
    try:
        for adapter_class in (BinanceFuturesAdapter, BybitFuturesAdapter, OkxFuturesAdapter):
            try:
                records = adapter_class().collect()
                if records:
                    inserted = insert_observations(conn, records)
                    logger.info(
                        "%s: %d записів (%d нових/змінених)",
                        adapter_class.source, len(records), inserted,
                    )
            except Exception:
                logger.error("%s: провалилось", adapter_class.source, exc_info=True)
                conn.rollback()
    finally:
        conn.close()


# Джерела, які ця джоба НЕ збирає сама — btc/eth/sol/xagusd (2026-10-02)
# уже збираються _crypto_prices()@щогодини.
_CRYPTO_OWNED_SOURCES = {"binance", "coingecko"}


def _watchlist_prices() -> None:
    """Збирає ціни watchlist-товарів/форексу/золота (FredAdapter для
    fred-джерел, TwelveDataAdapter для twelvedata-джерел,
    TradingEconomicsAdapter для tradingeconomics-джерел — 2026-10-04,
    commodities/tradingeconomics_adapter.py, скрапінг для товарів, яким
    FRED/Twelve Data не дають щоденної свіжості), ПОТІМ логує % зміни.
    Список активів — живий, з watchlist_assets (common/watchlist_db.py,
    2026-10-03, редагується через Telegram), не хардкод — ticker для
    twelvedata/tradingeconomics бере прямо з рядка БД (раніше окремий
    словник _TWELVEDATA_TICKER_BY_ASSET).

    Живо виявлено 2026-09-27 (перевірка "з нуля" на новому Docker
    Engine): ця функція раніше лише ЧИТАЛА fetch_all_price_changes(),
    жодного разу нічого не збираючи — попри щоденний розклад і
    докстрінг, що обіцяв збір. Єдиним (побічним, раз на місяць) шляхом
    ці дані взагалі потрапляли в БД був safety_net_collect_all
    (docs/decisions.md, 2026-09-27).

    Автоматичний резерв (2026-10-04, живий фідбек користувача:
    "резервний варіант має вже працювати... коли основне джерело не
    отримує свіжі дані то використовувати резерв") — `_FALLBACK_CHAINS`
    нижче: для xauusd/wti_crude/brent_crude/natgas пробує ВСІХ
    кандидатів щоразу й АВТОМАТИЧНО перемикає watchlist_assets.source
    на найсвіжішого (common/watchlist_db.py:choose_freshest_source()),
    без ручного `set_source()` чи сесії Claude."""
    sys.path.insert(0, str(REPO_ROOT / "analysis"))
    sys.path.insert(0, str(REPO_ROOT / "data-ingestion"))
    from commodities.tradingeconomics_adapter import TradingEconomicsAdapter  # noqa: E402
    from quotes.yahoo_chart_adapter import YahooChartAdapter  # noqa: E402
    from common.db import get_connection, insert_observations  # noqa: E402
    from common.consensus import SourceValue, reconcile  # noqa: E402
    from common.watchlist_db import (  # noqa: E402
        SourceCandidate, choose_freshest_source, fetch_price_sources, fetch_watchlist,
    )
    from config import CONSENSUS_TOLERANCE_PCT  # noqa: E402
    from crypto.binance_adapter import BinanceAdapter  # noqa: E402
    from macro.fred_adapter import FredAdapter  # noqa: E402
    from quotes.twelvedata_adapter import TwelveDataAdapter  # noqa: E402
    from news_analysis.prices import fetch_all_price_changes  # noqa: E402

    fred_key = os.environ.get("FRED_API_KEY")
    twelvedata_key = os.environ.get("TWELVEDATA_API_KEY")

    # Автоматичний резерв (2026-10-04, живий фідбек користувача:
    # "резервний варіант має вже працювати... коли основне джерело не
    # отримує свіжі дані то використовувати резерв") — для активів
    # нижче пробуємо ОБИДВА кандидати щоразу (не лише той, що зараз
    # активний у watchlist_assets), потім choose_freshest_source()
    # (common/watchlist_db.py) обирає найсвіжіший і сам перемикає
    # джерело, якщо треба — без ручного втручання. Перший у списку —
    # завжди бажаний (лишається активним, якщо сам свіжий).
    #
    # ⚠️ FRED ВИКИНУТО З ЛАНЦЮЖКІВ 2026-10-12. Цей механізм обирає
    # джерело ЗА СВІЖІСТЮ (choose_freshest_source) — і це була
    # небезпечна неповнота: свіжість нічого не каже про
    # ПРАВИЛЬНІСТЬ. Живий замір того дня: FRED давав Brent 135.51
    # проти справжніх 102.25 (викид 33%, правильне значення
    # підтверджене ручною звіркою по двох сайтах), WTI 97.89 проти
    # 91.11, газ 3.03 проти 3.14, каву 359.16 проти 287.89 (бо серія
    # місячна, з липня). Усі ці значення СВІЖІ — тобто ланцюжок
    # радісно перемкнувся б на них. Джерело, яке демонстровано
    # бреше, не може бути резервом; "резерв" із хибними числами
    # гірший за відсутність резерву.
    #
    # yahoo_chart — ПЕРШИЙ кандидат: у живому замірі збігся з
    # tradingeconomics у межах 0.3-1.4% (тобто два незалежні джерела
    # підтверджують одне одного) і додатково дає ІСТОРІЮ — 127-184
    # денні точки проти 6-7, без якої тренд-шар торгового списку був
    # сліпий на 7 із 10 активів.
    #
    # coffee ТЕПЕР ТУТ (раніше свідомо відсутня — "немає жодного
    # щоденного структурованого альтернативного джерела, FRED дає
    # лише МІСЯЧНУ PCOFFOTMUSDM"): Yahoo `KC=F` дає денну свічку,
    # тобто справжній щоденний резерв, а не вдаваний.
    _FALLBACK_CHAINS: dict[str, list] = {
        "xauusd": [
            SourceCandidate("yahoo_chart", "xauusd", None),
            SourceCandidate("tradingeconomics", "xauusd", "gold"),
            SourceCandidate("twelvedata", "xauusd", "XAU/USD"),
        ],
        "xagusd": [
            SourceCandidate("yahoo_chart", "xagusd", None),
        ],
        "wti_crude": [
            SourceCandidate("yahoo_chart", "wti_crude", None),
            SourceCandidate("tradingeconomics", "wti_crude", "crude-oil"),
        ],
        "brent_crude": [
            SourceCandidate("yahoo_chart", "brent_crude", None),
            SourceCandidate("tradingeconomics", "brent_crude", "brent-crude-oil"),
        ],
        "natgas": [
            SourceCandidate("yahoo_chart", "natgas", None),
            SourceCandidate("tradingeconomics", "natgas", "natural-gas"),
        ],
        "coffee": [
            SourceCandidate("yahoo_chart", "coffee", None),
            SourceCandidate("tradingeconomics", "coffee", "coffee"),
        ],
        "eurusd": [
            SourceCandidate("yahoo_chart", "eurusd", None),
            SourceCandidate("twelvedata", "eurusd", "EUR/USD"),
        ],
        "usdjpy": [
            SourceCandidate("yahoo_chart", "usdjpy", None),
            SourceCandidate("twelvedata", "usdjpy", "USD/JPY"),
        ],
    }

    def _collect_candidate(candidate: SourceCandidate):
        if candidate.source == "yahoo_chart":
            # limit=130 (а не 10, як у решти): цей адаптер — єдине
            # джерело ГЛИБИНИ для watchlist-активів, і тренд-шар
            # вимагає config.TREND_LONG_DAYS=60 точок. Append-only
            # дедуп робить повторний збір тих самих дат безпечним.
            return YahooChartAdapter(metric_id=candidate.metric_id).collect(limit=130)
        if candidate.source == "tradingeconomics":
            return TradingEconomicsAdapter(metric_id=candidate.metric_id, slug=candidate.ticker).collect()
        if candidate.source == "twelvedata":
            if not twelvedata_key:
                return []
            return TwelveDataAdapter(
                api_key=twelvedata_key, ticker=candidate.ticker, metric_id=candidate.metric_id,
            ).collect(limit=10)
        if candidate.source == "fred":
            if not fred_key:
                return []
            return FredAdapter(api_key=fred_key, metric_id=candidate.metric_id).collect(limit=10)
        return []

    def _exclude_outliers(conn, asset_id: str, chain: list) -> list:
        """Кандидати без тих, чиє останнє значення розходиться з
        рештою понад `config.CONSENSUS_TOLERANCE_PCT`.

        Якщо викидів немає, менше двох джерел, або згоди взагалі
        немає (`disagree` — двоє не згодні, переможця нічим
        визначити) — повертаємо ланцюжок як є: тут не місце
        відмовлятись від даних, завдання вужче — не дати ВИКИДУ
        стати активним джерелом."""
        values = []
        with conn.cursor() as cur:
            for candidate in chain:
                cur.execute(
                    """
                    SELECT value FROM raw_observations
                    WHERE source = %s AND metric_id = %s
                    ORDER BY observed_at DESC, revision DESC
                    LIMIT 1
                    """,
                    (candidate.source, candidate.metric_id),
                )
                row = cur.fetchone()
                if row is not None:
                    values.append(SourceValue(candidate.source, row[0]))

        tolerance = CONSENSUS_TOLERANCE_PCT.get(asset_id, CONSENSUS_TOLERANCE_PCT["default"])
        verdict = reconcile(asset_id, values, tolerance)
        if verdict.status != "outlier":
            return chain

        logger.warning(
            "%s: звірка джерел — %s; викид(и) виключено з вибору активного джерела",
            asset_id, verdict.detail,
        )
        remaining = [c for c in chain if c.source not in verdict.outliers]
        return remaining or chain

    conn = get_connection()
    try:
        watchlist = fetch_watchlist(conn)
        for row in watchlist:
            asset_id, source, metric_id, ticker = row["asset_id"], row["source"], row["metric_id"], row["ticker"]
            try:
                if source == "fred":
                    if not fred_key:
                        logger.error("%s: FRED_API_KEY не задано — пропущено", asset_id)
                        continue
                    records = FredAdapter(api_key=fred_key, metric_id=metric_id).collect(limit=10)
                elif source == "twelvedata":
                    if not twelvedata_key:
                        logger.error("%s: TWELVEDATA_API_KEY не задано — пропущено", asset_id)
                        continue
                    # metric_id=asset_id (2026-10-04, той самий принцип, що
                    # BinanceAdapter(metric_id=asset_id, ...) нижче): без
                    # цього normalize() вивів би базу metric_id з ТИКЕРА,
                    # який для активів, резолвлених через symbol_search
                    # (напр. /watchlist_add NATGAS -> тикер "NG"), не
                    # збігається з asset_id — зібрана ціна лягла б під
                    # "ng_close", тоді як watchlist_assets.metric_id і далі
                    # "natgas_close", і жодна функція, що читає
                    # watchlist_assets (prices.py/watchlist_notify.py/
                    # synthesize.py), ніколи б її не знайшла.
                    records = TwelveDataAdapter(
                        api_key=twelvedata_key, ticker=ticker, metric_id=asset_id,
                    ).collect(limit=10)
                elif source == "binance" and ticker:
                    # Крипто, додане ПІЗНІШЕ через Telegram /watchlist_add
                    # (2026-10-03, docs/decisions.md) — має ticker (символ
                    # Binance, напр. "BNBUSDT"), на відміну від
                    # оригінального btc/eth/sol (ticker=NULL, рядок нижче).
                    # symbol= в обхід фіксованого METRICS (binance_adapter.py).
                    # metric_id=asset_id (ГОЛИЙ, без "_close") — адаптер сам
                    # дописує "_close"/"_volume" у normalize(), той самий
                    # контракт, що оригінальний btc/eth/sol у _crypto_prices().
                    records = BinanceAdapter(metric_id=asset_id, symbol=ticker).collect(limit=10)
                elif source in _CRYPTO_OWNED_SOURCES:
                    # btc/eth/sol/xagusd (2026-10-02) — уже збираються
                    # _crypto_prices()@щогодини, не тут; без цього кожен
                    # прогін цієї джоби логував би хибне "невідоме
                    # джерело" на ці активи.
                    continue
                elif source == "yahoo_chart":
                    # Основне джерело watchlist-товарів/форексу з
                    # 2026-10-12: єдине, що дає і денну свіжість, і
                    # ГЛИБИНУ (limit=130 проти 10 у решти — тренд-шар
                    # вимагає config.TREND_LONG_DAYS=60 точок, і без
                    # цього список торгів друкував "історії недосить"
                    # на 7 із 10 активів).
                    records = YahooChartAdapter(metric_id=metric_id).collect(limit=130)
                elif source == "tradingeconomics":
                    # coffee/wti_crude/brent_crude/natgas (2026-10-04,
                    # critical rule 7) — справжній автоматизований
                    # скрапінг (commodities/tradingeconomics_adapter.py),
                    # щодня, без людини — замінив ручний web_crosscheck
                    # нижче, який вимагав LLM-сесію на кожне оновлення.
                    records = TradingEconomicsAdapter(metric_id=metric_id, slug=ticker).collect()
                elif source == "web_crosscheck":
                    # ОСТАННІЙ резерв (2026-10-04, critical rule 7) —
                    # лише коли НЕМАЄ жодного автоматизованого джерела
                    # (ні структурований API, ні скрапінг-адаптер типу
                    # tradingeconomics вище). Значення приходить ЗЗОВНІ
                    # (common/manual_observation.py, людина/LLM-веб-пошук
                    # звірений на 2+ сайтах) — без цієї гілки кожен
                    # прогін логував би хибне "невідоме джерело".
                    continue
                else:
                    logger.warning("%s: невідоме джерело %r — пропущено", asset_id, source)
                    continue

                if records:
                    inserted = insert_observations(conn, records)
                    logger.info("%s: %d записів (%d нових/змінених)", asset_id, len(records), inserted)
            except Exception:
                logger.error("%s: збір провалився", asset_id, exc_info=True)
                conn.rollback()

        # Резервний прохід (див. _FALLBACK_CHAINS вище) — ПІСЛЯ основного
        # циклу, який зібрав лише з ПОТОЧНОГО watchlist_assets.source.
        # Тут пробуємо КОЖНОГО кандидата в ланцюжку (включно з тим, що
        # вже зібраний вище, — повторний виклик нешкідливий,
        # insert_observation ідемпотентний) і обираємо найсвіжіший.


        for asset_id, chain in _FALLBACK_CHAINS.items():
            try:
                for candidate in chain:
                    try:
                        records = _collect_candidate(candidate)
                        if records:
                            insert_observations(conn, records)
                    except Exception:
                        logger.error(
                            "%s: резервний кандидат %s провалився", asset_id, candidate.source, exc_info=True,
                        )
                        conn.rollback()

                # ЗВІРКА ДЖЕРЕЛ перед вибором (2026-10-13). Свіжість
                # не каже нічого про правильність: усі хибні значення
                # FRED (Brent 135.51 проти справжніх 102.25) були
                # СВІЖІ, тож вибір "за свіжістю" радісно перемкнувся б
                # на брехню. Тепер звірка вирішує, КОМУ взагалі можна
                # вірити, і лише потім свіжість вибирає серед
                # довірених. Повне обґрунтування — common/consensus.py.
                trusted = _exclude_outliers(conn, asset_id, chain)
                chosen = choose_freshest_source(conn, asset_id, trusted)
                logger.info("%s: активне джерело -> %s", asset_id, chosen.source)
            except Exception:
                logger.error("%s: вибір резервного джерела провалився", asset_id, exc_info=True)
                conn.rollback()

        price_sources = fetch_price_sources(conn)
        changes = fetch_all_price_changes(conn, list(price_sources), price_sources=price_sources)
        for asset_id, change in changes.items():
            logger.info("%s: %.2f%% (%s -> %s)", asset_id, change.pct_change, change.start_date, change.end_date)
    finally:
        conn.close()


LOG_RETENTION_DAYS = 14


def _macro_daily_series() -> None:
    """Щоденний збір ДЕННИХ макро-серій FRED, у яких немає календаря
    релізів: облігації (`treasury_10y`/`treasury_2y`), ставка ФРС
    (`fed_funds_rate`), курс USD/JPY (`usdjpy_fx_rate`) —
    `macro/fred_adapter.py:DAILY_MACRO_METRICS`.

    Чому ця джоба існує (2026-10-04, рішення користувача "облігації це
    обовязково"): жоден щоденний конвеєр цих серій не брав.
    `check_releases` тригериться календарем релізів, якого в них не
    існує; `_watchlist_prices` бере лише watchlist-активи. Єдиним
    шляхом у БД був `safety_net_collect_all` — РАЗ НА МІСЯЦЬ, 1-го
    числа. Живий стан на 2026-10-04: treasury_10y/treasury_2y/
    fed_funds_rate стояли на 2026-09-29, тобто 5 днів застарілості на
    ДЕННОМУ показнику — те саме критичне правило 7 (CLAUDE.md), що й
    для watchlist-цін.

    Callable, не subprocess — як `_watchlist_prices`: потрібен один
    адаптер і один коміт, окремий CLI-скрипт для цього був би зайвим
    файлом (`collect_all.py --source fred` зібрав би ще й усі місячні
    серії даремно).

    append-only дедуп у `insert_observations()` робить повторний
    прогін безпечним: FRED публікує денні серії з лагом день-два, тож
    кілька прогонів поспіль бачать ті самі точки."""
    sys.path.insert(0, str(REPO_ROOT / "data-ingestion"))
    from common.db import get_connection, insert_observations  # noqa: E402
    from macro.fred_adapter import DAILY_MACRO_METRICS, FredAdapter  # noqa: E402

    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        raise RuntimeError("FRED_API_KEY не задано — денні макро-серії зібрати неможливо")

    conn = get_connection()
    try:
        for metric_id in sorted(DAILY_MACRO_METRICS):
            try:
                # limit=30 — щедрий запас на лаг публікації FRED
                # (день-два) і на пропущені прогони; append-only дедуп
                # робить перекриття безкоштовним.
                records = FredAdapter(api_key=api_key, metric_id=metric_id).collect(limit=30)
                inserted = insert_observations(conn, records) if records else 0
                logger.info(
                    "%s: зібрано %d точок (%d нових/змінених)",
                    metric_id, len(records), inserted,
                )
            except Exception:
                # Один показник не валить решту — той самий best-effort
                # принцип, що collect_all.py.
                logger.error("%s: збір провалився", metric_id, exc_info=True)
                conn.rollback()
    finally:
        conn.close()


def _prune_logs() -> None:
    """Видаляє файли logs/*.log, старші за LOG_RETENTION_DAYS.

    Один файл на джобу за добу (runner.py:_log_path, 2026-10-11) — за
    поточним розкладом це 54 файли на добу, тобто ~750 за вікно
    зберігання. До добового групування було по файлу на ВИКЛИК, і
    8 щохвилинних джоб давали ~12 000 файлів/добу (живий стан перед
    фіксом — 96 364 файли / 384 МБ).
    Логи — не джерело істини (rule 6 захищає сирі ДАНІ в БД, не
    раннтайм-вивід джоб; logs/ тому й у .gitignore), тож їх можна
    прибирати. 14 днів — достатньо, щоб розібрати будь-який провал
    постфактум, і достатньо мало, щоб диск не ріс нескінченно."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOG_RETENTION_DAYS)
    log_dir = REPO_ROOT / "logs"
    if not log_dir.is_dir():
        logger.info("logs/ ще не існує — нічого прибирати")
        return

    removed = 0
    for path in log_dir.glob("*.log"):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            # Один нечитабельний файл не повинен валити прибирання решти.
            logger.warning("не вдалось прибрати %s", path, exc_info=True)

    logger.info(
        "Прибрано %d логів, старших за %d днів (лишилось %d)",
        removed, LOG_RETENTION_DAYS, len(list(log_dir.glob("*.log"))),
    )


def _scheduler_heartbeat() -> None:
    """Власний "пульс" планувальника (docs/decisions.md, P0
    "Heartbeat планувальника") — UPSERT одного рядка з now() у
    scheduler_heartbeat (db/schema.sql). main.py звіряє його з
    поточним часом ПРИ СТАРТІ процесу: великий розрив означає, що
    контейнер/хост був недоступний ненормально довго, і про це варто
    дізнатись одразу, а не постфактум через запізнілий звіт (живий
    випадок 2026-10-02: NFP/Unemployment о 15:30 опрацьовано лише о
    17:57 — сам конвеєр check_releases→...→notify_expectations займає
    ~15 хв, причина затримки була в ~11-год простої scheduler, про яку
    ніхто не знав)."""
    sys.path.insert(0, str(REPO_ROOT / "data-ingestion"))
    from common.db import get_connection  # noqa: E402

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scheduler_heartbeat (id, last_tick_at)
                VALUES (1, now())
                ON CONFLICT (id) DO UPDATE SET last_tick_at = now()
                """
            )
        conn.commit()
    finally:
        conn.close()


def _prune_raw_news() -> None:
    """Видаляє raw_news, старші за RETENTION_HOURS (2026-09-28,
    рішення користувача — raw_news НЕ append-only-вічний, свідомий
    виняток із rule 6, common/news_db.py). Логіка/SQL — у
    data-ingestion/common/news_db.py (rule 1: orchestration лише
    викликає, нуль бізнес-логіки)."""
    sys.path.insert(0, str(REPO_ROOT / "data-ingestion"))
    from common.db import get_connection  # noqa: E402
    from common.news_db import prune_stale_news  # noqa: E402

    conn = get_connection()
    try:
        deleted = prune_stale_news(conn)
        logger.info("Видалено %d застарілих новин з raw_news", deleted)
    finally:
        conn.close()


# Колонка часу шукається серед цих, у такому порядку: таблиці проєкту
# називають її по-різному (`run_at` у знімкових, `discovered_at` у
# новачків, `checked_at` у перевірок здоров'я). Перелік явний, бо
# вгадувати "будь-яку timestamptz-колонку" означало б рано чи пізно
# прибрати дані за `notified_at`.
_TIME_COLUMN_CANDIDATES = ("run_at", "discovered_at", "checked_at", "created_at")


def _time_column(conn, table: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s AND column_name = ANY(%s)
            """,
            (table, list(_TIME_COLUMN_CANDIDATES)),
        )
        present = {row[0] for row in cur.fetchall()}
    for candidate in _TIME_COLUMN_CANDIDATES:
        if candidate in present:
            return candidate
    return None


def _prune_derived() -> None:
    """Прибирає ПОХІДНІ результати, старші за config.DERIVED_RETENTION_DAYS,
    і невикористані рядки `llm_call_log`.

    **Чому це не порушує критичне правило 6.** Правило захищає СИРІ
    ЗІБРАНІ дані (`raw_observations`) — вони лишаються append-only
    назавжди, їх ми лише СТИСКАЄМО (db/schema.sql, компресія з 30-го
    дня, зміряно 7.1×). Тут прибираються похідні висновки, які агент
    перерахує з сирих даних будь-коли.

    **Чому `llm_call_log` окремо.** Критичне правило 5 вимагає, щоб під
    КОЖНИМ збереженим висновком лишався його промпт. Тому видаляються
    лише рядки, старші за вікно І не згадані жодною з
    `config.LLM_CALL_REFERENCING_TABLES` (перевірено проти БД
    2026-10-15: саме ці 11 таблиць мають стовпець `llm_call_id`).

    **Що свідомо не прибирається:** `backtest_verdicts` (на них
    `method_selector` обирає ШІ vs naive), `metric_forecasts` і
    `decision_outcomes` (вимірювана історія ЯКОСТІ агента),
    `earnings_calendar`, `release_log`, `watchlist_assets`, `sources`.
    """
    sys.path.insert(0, str(REPO_ROOT / "data-ingestion"))
    sys.path.insert(0, str(REPO_ROOT))
    from common.db import get_connection  # noqa: E402

    import config  # noqa: E402

    conn = get_connection()
    total = 0
    try:
        for table, days in sorted(config.DERIVED_RETENTION_DAYS.items()):
            column = _time_column(conn, table)
            if column is None:
                # Таблиці може ще не бути (нова функція) — це не збій
                # прибирання, і решту воно забирати не має.
                logger.warning("%s: не знайшов колонки часу — пропускаю", table)
                continue
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {table} WHERE {column} < now() - (%s || ' days')::interval",  # noqa: S608
                    (days,),
                )
                deleted = cur.rowcount
            conn.commit()
            total += deleted
            logger.info("%s: видалено %d рядків (старші за %d днів)", table, deleted, days)

        # llm_call_log — лише те, на що ніхто не посилається (rule 5).
        refs = " UNION ALL ".join(
            f"SELECT llm_call_id FROM {t} WHERE llm_call_id IS NOT NULL"
            for t in config.LLM_CALL_REFERENCING_TABLES
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                DELETE FROM llm_call_log
                WHERE created_at < now() - (%s || ' days')::interval
                  AND id NOT IN ({refs})
                """,  # noqa: S608
                (config.LLM_CALL_LOG_RETENTION_DAYS,),
            )
            deleted = cur.rowcount
        conn.commit()
        total += deleted
        logger.info(
            "llm_call_log: видалено %d рядків (старші за %d днів і без посилань)",
            deleted, config.LLM_CALL_LOG_RETENTION_DAYS,
        )

        logger.info("Прибирання похідних: %d рядків усього", total)
    finally:
        conn.close()


JOBS = {
    # ОДИН конвеєр замість СЕМИ щохвилинних джоб (злито 2026-10-13):
    # check_releases → update_forecasts → compare_expectations →
    # synthesize_expectations → notify_expectations /
    # notify_release_impact / notify_forecasts.
    #
    # Було 7 × 1440 = 10 080 запусків Python-інтерпретера на добу, з
    # яких ~99.9% закінчувались "нічого робити" (релізів ~10 за 15
    # днів). Тепер один процес робить ОДИН дешевий SQL-запит "чи є
    # робота" і виходить, якщо її немає.
    #
    # Порядок кроків і контракт провалу не змінились — див.
    # orchestration/release_pipeline.py.
    "release_pipeline": {
        "subprocess": _py(str(REPO_ROOT / "orchestration" / "release_pipeline.py")),
    },
    "telegram_commands": {
        "subprocess": _py(str(REPO_ROOT / "orchestration" / "telegram_commands.py")),
    },
    "refresh_calendar": {
        "subprocess": _py(str(REPO_ROOT / "monitoring" / "refresh_calendar.py")),
    },
    "calendar_outlook": {
        "subprocess": _py(str(REPO_ROOT / "analysis" / "calendar_outlook" / "run_outlook.py")),
    },
    "notify_calendar_outlook": {
        "subprocess": _py(str(REPO_ROOT / "reporting" / "calendar_notify.py")),
    },
    "news_collect_watchlist": {
        # retries=3 СКАСОВАНО 2026-10-05 (повернуто до дефолтного 1).
        # Рішення 2026-09-29 ("ширше вікно, щоб ліміт звільнився") на
        # живих даних виявилось протилежним до потрібного: 4 внутрішні
        # спроби × 4 ретраї = до 16 запитів від ОДНІЄЇ джоби, і три
        # джоби збору разом давали ~100 запитів за кілька хвилин при
        # ліміті GDELT 1 запит/5с. 429 означає "ти ПОНАД лімітом" —
        # ретрай його не лікує, а поглиблює. Тепер ліміт тримає
        # глобальний обмежувач (common/rate_limit.py), а не ретраї.
        "subprocess": _py(str(REPO_ROOT / "data-ingestion" / "run_collect_news.py"), "--stream", "watchlist"),
    },
    "revalidate_news_terms": {
        # Самолікування: доводить до кінця live-перевірку новинних
        # термінів, яку не вдалось провести при додаванні активу через
        # Telegram (2026-10-06, живий фідбек — активи з Telegram не
        # потрапляли в новинний запит). Без ретраїв: сама джоба
        # ІДЕМПОТЕНТНА й повторюється щодня.
        "subprocess": _py(str(REPO_ROOT / "data-ingestion" / "revalidate_news_terms.py")),
    },
    "news_collect_rss": {
        "subprocess": _py(str(REPO_ROOT / "data-ingestion" / "run_collect_rss.py")),
    },
    "news_collect_stock": {
        # той самий принцип, що news_collect_watchlist/general вище —
        # теж GDELT, і найбільший споживач квоти (кілька груп запитів
        # на прогін), тому ретраї тут шкодили найдужче.
        "subprocess": _py(str(REPO_ROOT / "analysis" / "news_analysis" / "collect_stock_news.py")),
    },
    # ОДНА джоба замість трьох (`news_analysis_watchlist`/`_geopolitical`/
    # `_general`, злито 2026-10-13). Скрипт без `--stream` сам бере
    # непроаналізовані статті ВСІХ потоків одним запитом
    # (`_db.py:fetch_unanalyzed(stream=None)`), а `tracked_assets`
    # вибирається ПО СТАТТІ (`resolve_tracked_assets`), не по джобі —
    # тобто розділення на три процеси нічого не давало, крім трьох
    # записів у логах і трьох конекшенів до БД.
    "news_analysis": {
        "subprocess": _py(str(REPO_ROOT / "analysis" / "news_analysis" / "run_news_analysis.py")),
    },
    # ОДНА джоба замість трьох (злито 2026-10-13). Тут потоки
    # лишаються ОКРЕМИМИ LLM-викликами (різні промпти), але цикл
    # усередині скрипта з ізоляцією помилок по потоку
    # (`consolidate_one_stream`) — збій на одному не забирає решту,
    # як раніше гарантував сам факт трьох джоб.
    "news_consolidate": {
        "subprocess": _py(str(REPO_ROOT / "analysis" / "news_analysis" / "consolidate.py")),
    },
    "news_merge_similar": {
        # Семантичне об'єднання дублів МІЖ прогонами consolidate.py
        # (2026-09-29, живий приклад — 3 незалежні переклади "золото
        # впало на 7-тижневий мінімум" пішли 3 окремими записами;
        # text-similarity підтверджено ненадійним, потрібен окремий
        # фокусований LLM-виклик). Без --stream — усі 3 потоки.
        "subprocess": _py(str(REPO_ROOT / "analysis" / "news_analysis" / "merge_similar.py")),
    },
    "news_notify": {
        # Без --stream — ОДНЕ сповіщення з усіх зібраних потоків разом
        # (watchlist+general+geopolitical), рішення користувача
        # 2026-09-28: "важливі новини мають бути не тільки з watchlist
        # а зі всіх зібраних новин".
        "subprocess": _py(str(REPO_ROOT / "reporting" / "news_notify.py")),
    },
    "news_synthesis": {
        "subprocess": _py(str(REPO_ROOT / "analysis" / "news_analysis" / "synthesize.py")),
    },
    "notify_synthesis": {
        "subprocess": _py(str(REPO_ROOT / "reporting" / "synthesis_notify.py")),
    },
    # Три сесійні синтези замість одного добового (спек користувача
    # 2026-09-28). Три ОКРЕМІ джоби, не одна з аргументом: `schedule.py`
    # зберігає розклад на джобу, тож різний час = різні ключі.
    "market_synthesis_asia": {
        "subprocess": _py(
            str(REPO_ROOT / "analysis" / "news_analysis" / "synthesize_market.py"),
            "--session", "asia",
        ),
    },
    "market_synthesis_europe": {
        "subprocess": _py(
            str(REPO_ROOT / "analysis" / "news_analysis" / "synthesize_market.py"),
            "--session", "europe",
        ),
    },
    "market_synthesis_us": {
        "subprocess": _py(
            str(REPO_ROOT / "analysis" / "news_analysis" / "synthesize_market.py"),
            "--session", "us",
        ),
    },
    "notify_market_synthesis": {
        "subprocess": _py(str(REPO_ROOT / "reporting" / "market_notify.py")),
    },
    "policy_rate_synthesis": {
        "subprocess": _py(str(REPO_ROOT / "analysis" / "forecasting" / "synthesize_policy_rate.py")),
    },
    "notify_policy_rate": {
        "subprocess": _py(str(REPO_ROOT / "reporting" / "policy_rate_notify.py")),
    },
    "filing_insights": {
        # Звіти виходять раз на квартал, тож щодня тут нічого робити;
        # але сам прогін дешевий (`already_analyzed` відсікає вже
        # розібране до LLM-виклику), і тижневий ритм гарантує, що
        # новий 10-Q не чекатиме на наступний квартал.
        "subprocess": _py(str(REPO_ROOT / "analysis" / "fundamental" / "analyze_filings.py")),
    },
    "fundamental_analysis": {
        "subprocess": _py(str(REPO_ROOT / "analysis" / "fundamental" / "run_analysis.py")),
    },
    "notify_fundamental": {
        "subprocess": _py(str(REPO_ROOT / "reporting" / "fundamental_notify.py")),
    },
    "discover_candidates": {
        # Переписано 2026-10-05 з "LLM вигадує перспективну акцію з
        # новин" (news_analysis/discover_candidates.py, 0 рядків за
        # весь час) на три детектори руху — analysis/discovery/.
        # Назва джоби лишилась: під нею вже є Telegram-команда й
        # запис у розкладі.
        "subprocess": _py(str(REPO_ROOT / "analysis" / "discovery" / "run_discovery.py")),
    },
    "notify_candidates": {
        "subprocess": _py(str(REPO_ROOT / "reporting" / "candidates_notify.py")),
    },
    "daily_digest": {
        "subprocess": _py(str(REPO_ROOT / "reporting" / "daily_digest.py")),
    },
    "crypto_prices": {
        "callable": _crypto_prices,
    },
    "crypto_derivatives_collect": {
        "callable": _crypto_derivatives_collect,
    },
    "crypto_screening_daily": {
        # Живо виміряно 2026-09-27: ~3.5 хв на ~150 Tier A-виживших —
        # у межах дефолтного таймауту runner.py (30 хв), окремий не потрібен.
        "subprocess": _py(str(REPO_ROOT / "analysis" / "crypto_screening" / "run_screening.py")),
    },
    "crypto_candidates_monitor": {
        "subprocess": _py(str(REPO_ROOT / "analysis" / "crypto_screening" / "monitor_candidates.py")),
    },
    "watchlist_prices": {
        "callable": _watchlist_prices,
    },
    "screening_composite_score": {
        "subprocess": _py(str(REPO_ROOT / "analysis" / "screening" / "composite_score.py"), "--top", "10"),
    },
    "notify_screening": {
        "subprocess": _py(str(REPO_ROOT / "reporting" / "screening_notify.py")),
    },
    "notify_crypto_screening": {
        "subprocess": _py(str(REPO_ROOT / "reporting" / "crypto_screening_notify.py")),
    },
    "trading_list": {
        "subprocess": _py(str(REPO_ROOT / "analysis" / "trading_list" / "run_trading_list.py")),
    },
    "notify_trading_list": {
        "subprocess": _py(str(REPO_ROOT / "reporting" / "trading_list_notify.py")),
    },
    "notify_crypto_long": {
        "subprocess": _py(str(REPO_ROOT / "reporting" / "crypto_long_notify.py")),
    },
    "notify_watchlist": {
        "subprocess": _py(str(REPO_ROOT / "reporting" / "watchlist_notify.py")),
    },
    "companies_universe_refresh": {
        # Весь S&P 500 (503 тикери), SEC EDGAR — живо виміряно
        # 2026-09-27: ~26 хв, дефолтний runner.py timeout (30 хв) лишає
        # замало запасу на природний розкид латентності API. 50 хв.
        # retries=0: другий повний ~26-хв прогін одразу після провалу
        # (runner.py, дефолт — 1 ретрай) лише вдруге вперся б у те саме
        # джерело без паузи, і зсунув би наступну щотижневу джобу —
        # append-only дедуп все одно робить наступний ЗАПЛАНОВАНИЙ
        # прогін безпечним надолуженням.
        "subprocess": _py(str(REPO_ROOT / "data-ingestion" / "collect_companies_universe.py")),
        "timeout": 3000,
        "retries": 0,
    },
    "quotes_universe_refresh": {
        # Весь S&P 500 (503 тикери), Twelve Data — ліміт 8 запитів/хв
        # робить це НАЙПОВІЛЬНІШОЮ джобою в конвеєрі: живо виміряно
        # 2026-09-27 — ~78 хв. Дефолтний runner.py timeout (30 хв) робив
        # цю ЩОДЕННУ джобу приречена на TimeoutExpired щоразу (живий
        # провал, знайдений під час розгортання з нуля) — 90 хв дає
        # реальний запас. retries=0 — той самий принцип, що вище: другий
        # ~78-хв прогін одразу після провалу лише вдруге вичерпав би той
        # самий добовий ліміт Twelve Data без користі.
        "subprocess": _py(str(REPO_ROOT / "data-ingestion" / "collect_universe.py")),
        "timeout": 5400,
        "retries": 0,
    },
    "safety_net_collect_all": {
        "subprocess": _py(str(REPO_ROOT / "data-ingestion" / "collect_all.py"), "--limit", "15"),
    },
    "macro_daily_series": {
        "callable": _macro_daily_series,
    },
    "forecast_daily_series": {
        "subprocess": _py(str(REPO_ROOT / "analysis" / "forecasting" / "forecast_daily.py")),
    },
    "macro_crosscheck": {
        "subprocess": _py(
            str(REPO_ROOT / "monitoring" / "crosscheck_macro.py"), "--store", "--notify",
        ),
    },
    "prune_logs": {
        "callable": _prune_logs,
    },
    "prune_raw_news": {
        "callable": _prune_raw_news,
    },
    "prune_derived": {
        "callable": _prune_derived,
    },
    "evaluate_outcomes": {
        "subprocess": _py(str(REPO_ROOT / "monitoring" / "evaluate_outcomes.py")),
    },
    "notify_outcomes": {
        "subprocess": _py(str(REPO_ROOT / "reporting" / "outcomes_notify.py")),
    },
    "output_health": {
        "subprocess": _py(str(REPO_ROOT / "monitoring" / "output_health.py"), "--notify"),
    },
    "earnings_calendar": {
        "subprocess": _py(str(REPO_ROOT / "data-ingestion" / "run_collect_earnings.py")),
    },
    "quotes_crosscheck": {
        "subprocess": _py(str(REPO_ROOT / "monitoring" / "crosscheck_quotes.py"), "--notify"),
    },
    "scheduler_heartbeat": {
        "callable": _scheduler_heartbeat,
    },
}
