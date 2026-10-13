"""
Зчитування цінового руху watchlist-активів за вікно — звичайний код,
без LLM (analysis/CLAUDE.md: "числові порівняння — звичайний код").
Вхід для synthesize.py (зіставлення новинного сигналу з фактичним
рухом ціни — docs/decisions.md, ціль 1).

is_anomalous_move() (2026-10-02, живий фідбек користувача): синтез не
повинен залежати ЛИШЕ від того, чи є новина — рух ціни сам по собі теж
привід проаналізувати актив, навіть БЕЗ жодної новини. "Аномальний" —
не довільний відсоток (крипта рутинно рухається на 5%, золото рідко),
а відносно власної історичної волатильності активу: |pct_change за
вікно| проти очікуваного розмаху за той самий період, вивченого з
останніх спостережень (daily stdev × √window_days, "квадратний корінь
часу" — стандартне масштабування волатильності). Відкинуто: єдиний
фіксований % для всіх активів — підійшов би БТС, але золото/EURUSD
рідко рухаються на кілька % навіть за тиждень, тому дав би або шум на
крипті, або глухоту на решті.
"""

import statistics
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from common.db import fetch_recent
from common.freshness import is_stale as _is_stale

# Пороги аномального руху — з `config.py` (рішення користувача
# 2026-10-02: окремий файл для ручного редагування в продакшені).
# Тут лишається сама ЛОГІКА, не числа.
from config import (
    ANOMALY_HISTORY_LOOKBACK,
    ANOMALY_MIN_HISTORY,
    ANOMALY_SIGMA_MULTIPLIER,
)

# Критичне правило 7 (CLAUDE.md, 2026-10-04) — "свіжість даних,
# предмет постійної перевірки" стосується НЕ ЛИШЕ reporting/
# watchlist_notify.py (де вже є такий самий поріг), а й САМОГО аналізу:
# живий кейс користувача — synthesize.py видав висновок "Brent -5.01%"
# з пари точок 2026-09-28/29, хоча на момент прогону (2026-10-04) це
# вже 6-денної давнини дані. Корінь причини: fetch_price_change() нижче
# фільтрував лише "чи є 2+ точки ДЕСЬ у вікні `days`" — пара точок,
# застрягла на 6 днів тому, і далі технічно "в межах" 7-денного вікна,
# тому проходила як ніби свіжий сигнал. Поріг — `common/freshness.py:
# is_stale()` (той самий спільний модуль, що reporting/watchlist_notify.py
# імпортує — 2026-10-04, доповнення: "три дні це багато" — фіксовані 3
# КАЛЕНДАРНІ дні замінено на БУДНІ дні, щоб не карати форекс/товари за
# закритий у вихідні ринок, і водночас ловити 2-денну застарілість на
# буднях, яку 3 календарні дні пропускали б).

# asset_id (той самий, що news/queries.py:WATCHLIST_ASSET_IDS) →
# (source, metric_id) у raw_observations. Метал/товар/форекс ціни
# додані 2026-09-26 (docs/decisions.md).
#
# eurusd/usdjpy — Twelve Data (2026-10-02), НЕ FRED (DEXUSEU/DEXJPUS,
# стара серія usdjpy_fx_rate лишається в raw_observations НАЗАВЖДИ,
# rule 6 CLAUDE.md, просто більше не поточне джерело для цих
# asset_id): живо підтверджено на fred.stlouisfed.org — попри
# заявлену частоту "Daily", останнє спостереження ОБОХ серій
# застрягло на 2026-09-25, а "Next Release Date" на сторінці самого
# FRED — 2026-10-05 (10 днів розриву) — це затримка публікації на боці
# Fed H.10 release, не наш збір. Twelve Data вже підтверджено живо для
# форекс-символу зі слешем (xauusd нижче), EUR/USD і USD/JPY —
# мейнстрім-пари, мають бути як мінімум не гірше покриті.
#
# coffee/wti_crude/brent_crude/natgas — УСІ ЧОТИРИ товарні FRED-серії
# watchlist ПЕРЕМКНУТО на tradingeconomics (2026-10-04, critical rule 7
# CLAUDE.md, рішення користувача: "мені не потрібні фікси на окремі
# дані, мені потрібний один працюючий код — основне джерело + резерв +
# працювати тільки з свіжими даними"). coffee — FRED (PCOFFOTMUSDM,
# IMF) живо підтверджено МІСЯЧНА серія; wti_crude/brent_crude/natgas —
# формально ЩОДЕННІ FRED-серії, але живо підтверджено жодна НЕ
# публікувала нових точок 3+ бізнес-дні поспіль (застрягли на
# 2026-09-29 одночасно). Проміжний крок (ручний крос-чек,
# source="web_crosscheck", common/manual_observation.py) вимагав LLM-
# сесію на КОЖНЕ оновлення — протримався лише кілька годин цієї сесії.
# Фінальне джерело — commodities/tradingeconomics_adapter.py: справжній
# автоматизований скрапінг tradingeconomics.com/commodity/<slug> (ціна+
# дата з <meta name="description">, живо підтверджено стабільний
# формат, requests.get() без JS/бот-захисту), на розкладі
# `_watchlist_prices`, ЩОДНЯ, без людини — закриває прогалину
# НАЗАВЖДИ, не разово. `web_crosscheck` лишається ОСТАННІМ резервом
# (db/schema.sql) для гіпотетичного майбутнього активу, якому немає
# жодного автоматизованого джерела взагалі. reporting/watchlist_notify.py
# додатково й надалі АКТИВНО позначає застарілі рядки
# (common/freshness.py:is_stale()) — захист навіть якщо
# tradingeconomics колись сам застрягне. Стара fred-історія
# лишається в raw_observations назавжди для всіх чотирьох (rule 6).
#
# xauusd — ТЕЖ tradingeconomics (2026-10-04), але ІНША причина, не
# застарілість (twelvedata оновлювався щодня): живий фідбек
# користувача — синтез порахував "+0.55%" за тиждень, коли золото
# РЕАЛЬНО впало. Корінь — Twelve Data forex-OTC котирування нестабільне
# ЗАДНІМ ЧИСЛОМ: той самий 2026-09-28 отримав дві ревізії з різницею
# 81 пункт (4196.13 -> 4115.08), і `v_observations_latest_revision`
# бере останню, не обов'язково точнішу (звірка з investing.com
# futures показала — жодна з двох ревізій не була стабільно ближчою
# до реальності, це властивість джерела — немає єдиного офіційного
# сетлменту в OTC, не помилка парсингу). TradingEconomics дає ОДНЕ
# число на день без цього внутрішнього дрейфу. Стара twelvedata-
# історія (metric_id="xauusd_close") лишається назавжди (rule 6).
#
# btc/eth/sol — crypto/binance_adapter.py (metric_id=f"{id}_close",
# source="binance"), той самий ключ, що WATCHLIST_ASSET_IDS
# (news/queries.py). xagusd — Twelve Data XAG/USD вимагає платний план
# (docs/decisions.md 2026-09-26), тому НЕ twelvedata: ціна йде через
# crypto/coingecko_adapter.py (kinesis-silver, токен-проксі 1:1 до
# фізичного срібла, metric_id="xagusd_close", source="coingecko") —
# той самий адаптер, що й market cap крипти нижче.
#
# btc/eth/sol/xagusd додані 2026-10-02: до цього _resolve_price_source()
# для них помилково гадав twelvedata-конвенцію (fallback нижче), де
# даних для жодного з них ніколи не було, тому fetch_price_change()
# завжди повертав None — синтез (synthesize.py) ЦІЛКОМ пропускав усі
# чотири активи watchlist, хоча ціна насправді вже збиралась.
ASSET_PRICE_SOURCES: dict[str, tuple[str, str]] = {
    "xauusd": ("tradingeconomics", "xauusd"),
    "xagusd": ("coingecko", "xagusd_close"),
    "wti_crude": ("tradingeconomics", "wti_crude"),
    "brent_crude": ("tradingeconomics", "brent_crude"),
    "eurusd": ("twelvedata", "eurusd_close"),
    "coffee": ("tradingeconomics", "coffee"),
    "natgas": ("tradingeconomics", "natgas"),
    "usdjpy": ("twelvedata", "usdjpy_close"),
    "btc": ("binance", "btc_close"),
    "eth": ("binance", "eth_close"),
    "sol": ("binance", "sol_close"),
}


def _resolve_price_source(asset_id: str, price_sources: dict[str, tuple[str, str]] = None) -> tuple[str, str]:
    """Для watchlist-товарів/форексу — явний мапінг (`price_sources`,
    None=дефолт падає на ASSET_PRICE_SOURCES нижче — викликач, що хоче
    ЖИВИЙ, редагований через Telegram список, передає
    `common/watchlist_db.py:fetch_price_sources(conn)` явно, 2026-10-03,
    docs/decisions.md). Для БУДЬ-ЯКОГО іншого asset_id (тикери акцій зі
    скринінгу, docs/decisions.md "Ціль 2") — здогад за конвенцією,
    якою quotes/twelvedata_adapter.py сам будує metric_id
    (`f"{ticker.lower()}_close"`). Безпечно: якщо здогад хибний (актив
    насправді з іншого джерела, напр. крипта) — просто не знайдеться
    жодного спостереження, fetch_price_change() поверне None, як і
    раніше."""
    effective_sources = price_sources if price_sources is not None else ASSET_PRICE_SOURCES
    mapping = effective_sources.get(asset_id)
    if mapping is not None:
        return mapping
    return ("twelvedata", f"{asset_id.lower()}_close")


@dataclass
class PriceChange:
    asset_id: str
    start_value: Decimal
    end_value: Decimal
    start_date: str
    end_date: str
    pct_change: Decimal


def compute_pct_change(
    asset_id: str, start_value: Decimal, start_date: str, end_value: Decimal, end_date: str
) -> PriceChange:
    """Чиста функція — з готових значень рахує % зміни. Розрахунок
    відокремлено від SQL (fetch_price_change нижче), щоб тестувалось
    без БД, той самий принцип, що й passes_tier_*/decide_revision в
    інших модулях проєкту."""
    pct_change = ((end_value - start_value) / start_value) * Decimal("100") if start_value != 0 else Decimal("0")
    return PriceChange(
        asset_id=asset_id,
        start_value=start_value,
        end_value=end_value,
        start_date=start_date,
        end_date=end_date,
        pct_change=pct_change,
    )


def fetch_price_change(
    conn, asset_id: str, days: int = 7, price_sources: dict[str, tuple[str, str]] = None
) -> Optional[PriceChange]:
    """Перше й останнє значення (v_observations_latest_revision) за
    останні `days` днів для asset_id — None, якщо немає даних у вікні
    (для watchlist-товарів/форексу — price_sources/ASSET_PRICE_SOURCES;
    для інших asset_id, напр. тикерів акцій — здогад
    _resolve_price_source(), теж природно дає None за відсутності
    даних), І None, якщо найновіша точка сама застаріла
    (common/freshness.py:is_stale() — докстрінг модуля вище) — вікно
    `days` саме по собі НЕ гарантує свіжості: пара точок, застрягла
    6 днів тому, технічно "в межах" 7-денного вікна, але вже не
    відображає поточний стан ринку. Викликач (synthesize.py) трактує
    None так само, як "даних у вікні немає" — fetch_latest_observed_at()
    окремо дає чесне "ЗАСТАРІЛА" замість мовчазного пропуску."""
    source, metric_id = _resolve_price_source(asset_id, price_sources)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT observed_at, value
            FROM v_observations_latest_revision
            WHERE source = %s AND metric_id = %s
              AND observed_at >= now() - (%s || ' days')::interval
            ORDER BY observed_at ASC
            """,
            (source, metric_id, days),
        )
        rows = cur.fetchall()

    if len(rows) < 2:
        return None

    (start_date, start_value), (end_date, end_value) = rows[0], rows[-1]
    if _is_stale(end_date):
        return None

    return compute_pct_change(
        asset_id, Decimal(start_value), str(start_date), Decimal(end_value), str(end_date)
    )


def fetch_all_price_changes(
    conn, asset_ids: list[str], days: int = 7, price_sources: dict[str, tuple[str, str]] = None
) -> dict[str, PriceChange]:
    """Те саме для списку активів одразу — пропускає ті, для яких
    немає джерела чи даних (не помилка, просто немає числового
    контексту для цього активу поки що)."""
    result = {}
    for asset_id in asset_ids:
        change = fetch_price_change(conn, asset_id, days=days, price_sources=price_sources)
        if change is not None:
            result[asset_id] = change
    return result


def fetch_latest_observed_at(conn, asset_id: str, price_sources: dict[str, tuple[str, str]] = None):
    """Дата НАЙСВІЖІШОГО спостереження для asset_id, БЕЗ обмеження
    вікном `days` (на відміну від fetch_price_change) — або None, якщо
    для цього джерела взагалі ще ніколи нічого не збирали.

    Призначення (2026-09-29, живий фідбек користувача): коли
    fetch_price_change() повертає None, це раніше означало ОДНЕ й те
    саме "немає джерела/даних" і для "збір ніколи не запускався", і для
    "збір є, але завис/відстає" (напр. FRED-серія тижневої давності)
    — ці два випадки принципово різні для діагностики, тому
    synthesize.py викликає цю функцію окремо, щоб сказати чесно, ЯКИЙ
    це випадок."""
    source, metric_id = _resolve_price_source(asset_id, price_sources)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(observed_at)
            FROM v_observations_latest_revision
            WHERE source = %s AND metric_id = %s
            """,
            (source, metric_id),
        )
        (latest,) = cur.fetchone()
    return latest


def fetch_price_history(
    conn,
    asset_id: str,
    limit: int = ANOMALY_HISTORY_LOOKBACK,
    price_sources: dict[str, tuple[str, str]] = None,
) -> list[Decimal]:
    """Останні `limit` значень asset_id у ХРОНОЛОГІЧНОМУ порядку
    (найстаріше перше) — вхід для is_anomalous_move(). Перевикористовує
    common.db.fetch_recent() (той самий запит, що forecasting/backtest.py
    вже використовує для показників) замість власного SQL тут.

    `price_sources` додано 2026-10-13 — ЛАТЕНТНИЙ БАГ. Функція
    викликала `_resolve_price_source(asset_id)` без цього аргументу,
    тож завжди падала на ХАРДКОД `ASSET_PRICE_SOURCES`, ігноруючи
    живий `watchlist_assets`. Поки обидва збігались, це було
    непомітно. Але 2026-10-12 watchlist перевели на `yahoo_chart`
    (127-251 точка історії), а хардкод лишився на
    `tradingeconomics`/`coingecko` з 6-7 точками — тобто детектор
    аномалій читав рівно той ряд, на якому `ANOMALY_MIN_HISTORY`=10
    ніколи не виконується, і чесно повертав False ЗАВЖДИ. Той самий
    клас помилки, що сліпий тренд-шар торгового списку: не падає, не
    логує, просто тихо нічого не знаходить.

    Викликач, що має живий список (`fetch_price_sources(conn)`), тепер
    мусить його передавати."""
    source, metric_id = _resolve_price_source(asset_id, price_sources)
    rows = fetch_recent(conn, source, metric_id, limit=limit)  # найновіше перше
    return [Decimal(r["value"]) for r in reversed(rows)]


def is_anomalous_move(
    pct_change: Decimal,
    history: list[Decimal],
    window_days: int,
    min_history: int = ANOMALY_MIN_HISTORY,
    sigma_multiplier: float = ANOMALY_SIGMA_MULTIPLIER,
) -> bool:
    """Чи є |pct_change| за `window_days` незвичним для ЦЬОГО активу,
    судячи з його власної історії `history` (fetch_price_history(),
    хронологічний порядок). Деталі підходу — докстрінг модуля вище.

    `False` (не аномально), якщо історії замало (min_history) ЧИ
    волатильність нульова (stdev=0, напр. усі значення однакові) —
    чесний дефолт "немає статистичної опори судити", не false positive."""
    daily_returns = [
        float((history[i] - history[i - 1]) / history[i - 1]) * 100
        for i in range(1, len(history))
        if history[i - 1] != 0
    ]
    if len(daily_returns) < min_history:
        return False

    daily_sigma = statistics.stdev(daily_returns)
    if daily_sigma == 0:
        return False

    expected_sigma_over_window = daily_sigma * (window_days ** 0.5)
    return abs(float(pct_change)) >= sigma_multiplier * expected_sigma_over_window
