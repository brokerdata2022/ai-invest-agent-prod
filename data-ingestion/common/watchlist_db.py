"""
Watchlist — активи поза акціями S&P 500 (docs/watchlist.md, закрито
2026-09-25): ОДНЕ джерело істини (таблиця watchlist_assets,
db/schema.sql), яке читають news/queries.py (GDELT query),
analysis/news_analysis/ (prices.py/consolidate.py/synthesize.py/
run_news_analysis.py — ціна↔новини, tracked_assets для DeepSeek),
orchestration/jobs.py (_watchlist_prices) і reporting/watchlist_notify.py
(докстрінг кожного з них посилається сюди).

Редагування через Telegram (orchestration/telegram_commands.py,
docs/decisions.md 2026-10-03) обмежено `DYNAMICALLY_ADDABLE_SOURCES` —
лише ті джерела, де тикер/символ — параметр адаптера (на зараз тільки
`quotes/twelvedata_adapter.py`), не фіксований словник METRICS
(fred/binance/coingecko) — новий актив у ЦИХ джерелах і далі вимагає
коду, Telegram-команда прямо каже про це, не вдає можливість, якої
немає.
"""

import json
import logging
from typing import NamedTuple, Optional

from common.freshness import is_stale

logger = logging.getLogger(__name__)


class SourceCandidate(NamedTuple):
    source: str
    metric_id: str
    ticker: Optional[str] = None

DYNAMICALLY_ADDABLE_SOURCES = frozenset({"twelvedata"})

# `search_term_status` у наборі обов'язково (2026-10-06): fetch_terms()
# фільтрує за ним, і без колонки в SELECT фільтр тихо пропускав би ВСІ
# терміни (dict.get -> None).
_COLUMNS = (
    "asset_id, source, metric_id, ticker, label, search_term, "
    "search_term_status, enabled, "
    # Профіль активу (2026-10-22, db/schema.sql): тип і все, що доти
    # жило хардкодом у семи місцях коду.
    "asset_class, commodity_group, company_name, cik, "
    "news_patterns, ask_patterns, price_chain"
)

# Колонки профілю, які можна записати `set_profile()` / `add_asset()`.
PROFILE_FIELDS = (
    "asset_class", "commodity_group", "company_name", "cik",
    "news_patterns", "ask_patterns", "price_chain",
)


def fetch_watchlist(conn, enabled_only: bool = True) -> list[dict]:
    """Усі рядки watchlist_assets (чи лише enabled, дефолт) — той
    самий ефективний список, що раніше малося на увазі хардкодженими
    словниками (увесь попередній список був "enabled")."""
    query = f"SELECT {_COLUMNS} FROM watchlist_assets"
    if enabled_only:
        query += " WHERE enabled"
    query += " ORDER BY asset_id"
    with conn.cursor() as cur:
        cur.execute(query)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def fetch_asset_ids(conn, enabled_only: bool = True) -> list[str]:
    """Той самий перелік, що раніше news/queries.py:WATCHLIST_ASSET_IDS
    (список ключів) — тепер із БД, не з хардкодженого словника."""
    return [row["asset_id"] for row in fetch_watchlist(conn, enabled_only=enabled_only)]


# Статуси новинного терміну (db/schema.sql:
# watchlist_assets.search_term_status, 2026-10-06).
TERM_VALIDATED = "validated"
TERM_PENDING = "pending"
TERM_REJECTED = "rejected"


def fetch_terms(conn, enabled_only: bool = True) -> dict[str, str]:
    """Той самий словник, що раніше news/queries.py:WATCHLIST_TERMS.

    У спільний GDELT-запит потрапляють ЛИШЕ терміни зі статусом
    'validated' (2026-10-04, живий кейс "BNB": один поганий OR-термін
    псував запит для ВСІХ watchlist-активів одразу).

    2026-10-06: фільтр за СТАТУСОМ, не за `search_term IS NULL`.
    Різниця принципова — тепер термін зберігається навіть коли
    перевірку не вдалось провести ('pending'), і джоба
    `revalidate_news_terms` доводить його до кінця. Доти "не вдалось
    перевірити" було нерозрізнимим від "відмовлено назавжди", і з
    баном GDELT кожен новий актив мовчки лишався без новин."""
    return {
        row["asset_id"]: row["search_term"]
        for row in fetch_watchlist(conn, enabled_only=enabled_only)
        if row["search_term"] and row.get("search_term_status") == TERM_VALIDATED
    }


def fetch_watchlist_health(conn) -> list[dict]:
    """Повний стан кожного активу списку обраних ОДНИМ запитом: джерело,
    остання ціна з датою, статус новинного терміну.

    Додано 2026-10-06 після прямого фідбеку користувача: "ти маєш
    контролювати список обраних, а не в логах шукати додані активи".
    Доти `/watchlist_list` показував лише назву й джерело, тож стан
    власного списку (чи свіжа ціна, чи бере актив участь у новинному
    запиті) з бота не було видно взагалі — і перевірити це можна було
    лише ззовні, руками. Тепер усе, що потрібно для відповіді "що з
    моїм списком", віддає ця функція, а рендерить
    `telegram_commands.format_watchlist_list()`.

    LEFT JOIN — актив без жодної зібраної ціни (щойно доданий, або
    джерело відмовило) мусить БУТИ У СПИСКУ з порожньою ціною, а не
    зникнути з нього."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT w.asset_id, w.label, w.source, w.metric_id, w.enabled,
                   w.search_term, w.search_term_status, w.added_via,
                   w.asset_class, w.commodity_group,
                   p.value AS price, p.observed_at
            FROM watchlist_assets w
            LEFT JOIN (
                SELECT DISTINCT ON (source, metric_id) source, metric_id, value, observed_at
                FROM v_observations_latest_revision
                ORDER BY source, metric_id, observed_at DESC
            ) p ON p.source = w.source AND p.metric_id = w.metric_id
            ORDER BY w.enabled DESC, w.asset_id
            """
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def fetch_pending_terms(conn) -> list[dict]:
    """Активи, чий новинний термін чекає на повторну перевірку —
    вхід для джоби `revalidate_news_terms`."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asset_id, label, search_term
            FROM watchlist_assets
            WHERE enabled AND search_term IS NOT NULL
              AND search_term_status = %s
            ORDER BY asset_id
            """,
            (TERM_PENDING,),
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def set_term_status(conn, asset_id: str, status: str) -> None:
    """Переводить термін у 'validated'/'rejected' після повторної
    перевірки. Сам термін не чіпається — він лишається в БД і для
    'rejected' теж: інакше наступна перевірка не знала б, що саме
    пробувалось."""
    if status not in (TERM_VALIDATED, TERM_PENDING, TERM_REJECTED):
        raise ValueError(f"Невідомий статус терміну: {status!r}")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE watchlist_assets SET search_term_status = %s WHERE asset_id = %s",
            (status, asset_id),
        )
    conn.commit()


def fetch_price_sources(conn, enabled_only: bool = True) -> dict[str, tuple[str, str]]:
    """Той самий словник, що раніше
    analysis/news_analysis/prices.py:ASSET_PRICE_SOURCES."""
    return {row["asset_id"]: (row["source"], row["metric_id"]) for row in fetch_watchlist(conn, enabled_only=enabled_only)}


def find_asset(conn, asset_id: str) -> Optional[dict]:
    """Рядок (включно з вимкненими) за asset_id, або None — для
    перевірки "вже є"/"взагалі існує" перед add/remove у
    telegram_commands.py."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM watchlist_assets WHERE asset_id = %s", (asset_id,))
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cur.description]
        return dict(zip(columns, row))


def add_asset(
    conn, asset_id: str, source: str, metric_id: str,
    ticker: Optional[str], label: str, search_term: Optional[str],
    search_term_status: str = TERM_VALIDATED,
    profile: Optional[dict] = None,
) -> None:
    """Викликач (telegram_commands.py) МАЄ перевірити find_asset() is
    None заздалегідь для зрозумілого повідомлення користувачу — тут
    просто INSERT, порушення PRIMARY KEY кидається як є.

    `search_term_status` (2026-10-06) — 'validated' / 'pending' /
    'rejected'. У спільний GDELT-запит іде лише 'validated'
    (fetch_terms() вище), але сам термін зберігається й для 'pending':
    інакше "перевірку не вдалось провести" було б нерозрізнимим від
    "відмовлено назавжди", і з баном GDELT актив мовчки лишався б без
    новин назавжди (живий фідбек користувача 2026-10-06)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO watchlist_assets
                (asset_id, source, metric_id, ticker, label, search_term,
                 search_term_status, added_via)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'telegram')
            """,
            (asset_id, source, metric_id, ticker, label, search_term, search_term_status),
        )
    conn.commit()
    logger.info("watchlist_assets: додано %s (%s/%s)", asset_id, source, metric_id)
    if profile:
        set_profile(conn, asset_id, profile)


def set_profile(conn, asset_id: str, profile: dict) -> bool:
    """Записати поля профілю активу (`PROFILE_FIELDS`). Невідомий ключ —
    виняток: тихо проігнорована колонка — рівно той клас дефектів,
    від якого цей профіль і рятує. `price_chain` — список словників,
    пишеться як JSONB."""
    unknown = set(profile) - set(PROFILE_FIELDS)
    if unknown:
        raise ValueError(f"Невідомі поля профілю активу: {sorted(unknown)}")
    if not profile:
        return False
    fields = list(profile)
    values = [
        json.dumps(profile[f], ensure_ascii=False) if f == "price_chain" and profile[f] is not None
        else profile[f]
        for f in fields
    ]
    assignments = ", ".join(
        f"{f} = %s::jsonb" if f == "price_chain" else f"{f} = %s" for f in fields
    )
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE watchlist_assets SET {assignments} WHERE asset_id = %s",
            (*values, asset_id),
        )
        updated = cur.rowcount > 0
    conn.commit()
    if updated:
        logger.info("watchlist_assets: %s профіль %s", asset_id, ", ".join(fields))
    return updated


def price_chain(row: dict) -> list[SourceCandidate]:
    """Кандидати джерела ціни В ПОРЯДКУ ПРІОРИТЕТУ з `price_chain`
    рядка. Порожній ланцюжок — лише власне джерело рядка.

    Замінює `orchestration/jobs.py:_FALLBACK_CHAINS` (хардкод на 8
    активів, 2026-10-04…22): актив, доданий через Telegram, резерву не
    мав ніколи."""
    chain = row.get("price_chain") or []
    if isinstance(chain, str):
        chain = json.loads(chain)
    candidates = [
        SourceCandidate(item["source"], item["metric_id"], item.get("ticker"))
        for item in chain
        if item.get("source") and item.get("metric_id")
    ]
    return candidates or [SourceCandidate(row["source"], row["metric_id"], row.get("ticker"))]


def fetch_patterns(conn, column: str, enabled_only: bool = True) -> dict[str, tuple[str, ...]]:
    """{asset_id: регулярки} з `news_patterns` чи `ask_patterns` —
    замість `config.WATCHLIST_MATCH_TERMS` / `ASSISTANT_ASSET_TERMS`.
    Актив без термінів у словник не потрапляє."""
    if column not in ("news_patterns", "ask_patterns"):
        raise ValueError(f"Невідома колонка термінів: {column!r}")
    return {
        row["asset_id"]: tuple(row[column])
        for row in fetch_watchlist(conn, enabled_only=enabled_only)
        if row.get(column)
    }


def fetch_stocks(conn, enabled_only: bool = True) -> list[dict]:
    """Акції списку обраних — їм потрібна звітність SEC, тексти
    10-K/10-Q і новини по тикеру, як акціям зі скринінгу."""
    return [
        row for row in fetch_watchlist(conn, enabled_only=enabled_only)
        if row.get("asset_class") == "stock" and row.get("ticker")
    ]


def set_search_term(conn, asset_id: str, search_term: Optional[str]) -> bool:
    """Виправити/прибрати (None) GDELT-термін уже доданого активу —
    напр. якщо термін виявився проблемним ПІСЛЯ додавання (живий
    кейс "BNB", 2026-10-04) і потребує корекції без видалення й
    повторного додавання всього рядка. Повертає False, якщо asset_id
    не знайдено."""
    with conn.cursor() as cur:
        cur.execute("UPDATE watchlist_assets SET search_term = %s WHERE asset_id = %s", (search_term, asset_id))
        updated = cur.rowcount > 0
    conn.commit()
    if updated:
        logger.info("watchlist_assets: %s search_term=%r", asset_id, search_term)
    return updated


def set_source(
    conn, asset_id: str, source: str, metric_id: Optional[str] = None, ticker: Optional[str] = None,
) -> bool:
    """Перемкнути джерело вже доданого активу (2026-10-04, живий урок:
    попередні три міграції wti_crude/brent_crude/coffee/natgas на
    web_crosscheck редагувались через `db/schema.sql` UPDATE-рядки,
    що вимагало `apply_schema.py` — забутий ОДИН раз (natgas) дав
    "фікс", який насправді нічого не змінив у живій БД, користувач
    отримав ту саму застарілу ціну вдруге). Відтепер перемикання
    джерела — ЗВИЧАЙНА операція над даними (як set_enabled/
    set_search_term), не зміна схеми — нічого забути застосувати.
    `metric_id`/`ticker=None` (дефолт) лишає поточне значення
    незмінним, передайте явно лише те, що міняється. Повертає False,
    якщо asset_id не знайдено."""
    with conn.cursor() as cur:
        if metric_id is not None and ticker is not None:
            cur.execute(
                "UPDATE watchlist_assets SET source = %s, metric_id = %s, ticker = %s WHERE asset_id = %s",
                (source, metric_id, ticker, asset_id),
            )
        elif metric_id is not None:
            cur.execute(
                "UPDATE watchlist_assets SET source = %s, metric_id = %s WHERE asset_id = %s",
                (source, metric_id, asset_id),
            )
        elif ticker is not None:
            cur.execute(
                "UPDATE watchlist_assets SET source = %s, ticker = %s WHERE asset_id = %s",
                (source, ticker, asset_id),
            )
        else:
            cur.execute("UPDATE watchlist_assets SET source = %s WHERE asset_id = %s", (source, asset_id))
        updated = cur.rowcount > 0
    conn.commit()
    if updated:
        logger.info("watchlist_assets: %s source=%r (metric_id=%r, ticker=%r)", asset_id, source, metric_id, ticker)
    return updated


def choose_freshest_source(conn, asset_id: str, candidates: list[SourceCandidate]) -> SourceCandidate:
    """Автоматичний резерв (2026-10-04, живий фідбек користувача:
    "резервний варіант має вже працювати... коли основне джерело не
    отримує свіжі дані то використовувати резерв") — `candidates` у
    ПОРЯДКУ пріоритету (перший — бажаний, якщо свіжий); функція
    припускає, що викликач (orchestration/jobs.py:_watchlist_prices)
    ВЖЕ спробував зібрати дані з КОЖНОГО кандидата цим прогоном —
    тут лише ОБИРАЄМО, чиє значення читати далі, за свіжістю
    (common/freshness.py:is_stale(), БУДНІ дні — вихідні НЕ
    тригерять перемикання на резерв, ринок просто закритий).

    Побічний ефект: якщо обране джерело відрізняється від поточного
    `watchlist_assets.source` — перемикає (`set_source()`), щоб ВСІ
    подальші читачі (reporting/watchlist_notify.py,
    analysis/news_analysis/prices.py, synthesize.py) автоматично
    пішли на актуальне джерело, без ручного втручання чи
    `apply_schema.py`.

    Якщо ЖОДЕН кандидат не свіжий (усі застарілі) — повертає перший
    (статус-кво, не змінює джерело): немає кращого варіанту просто
    зараз, але `common/freshness.py:is_stale()`-позначка в звіті
    однаково покаже застарілість — чесно, не вдає свіжість, якої
    немає."""
    chosen = candidates[0]
    for candidate in candidates:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(observed_at) FROM raw_observations WHERE source = %s AND metric_id = %s",
                (candidate.source, candidate.metric_id),
            )
            (latest,) = cur.fetchone()
        if latest is not None and not is_stale(latest):
            chosen = candidate
            break

    current = find_asset(conn, asset_id)
    if current is not None and (current["source"], current["metric_id"]) != (chosen.source, chosen.metric_id):
        logger.info(
            "watchlist_assets: %s автоматично перемкнуто %s -> %s (резерв свіжіший)",
            asset_id, current["source"], chosen.source,
        )
        set_source(conn, asset_id, chosen.source, metric_id=chosen.metric_id, ticker=chosen.ticker)
    return chosen


def set_enabled(conn, asset_id: str, enabled: bool) -> bool:
    """Повертає False, якщо asset_id взагалі не знайдено (немає що
    вимикати/включати) — викликач вирішує, що відповісти користувачу."""
    with conn.cursor() as cur:
        cur.execute("UPDATE watchlist_assets SET enabled = %s WHERE asset_id = %s", (enabled, asset_id))
        updated = cur.rowcount > 0
    conn.commit()
    if updated:
        logger.info("watchlist_assets: %s enabled=%s", asset_id, enabled)
    return updated


# Похідні таблиці, з яких актив прибирається при видаленні (2026-10-22,
# пункт 2 плану сесії: "видалення = видалення"). (таблиця, колонка, умова)
# — умова "watchlist" обмежує рядки, що походять саме зі списку обраних.
_DERIVED_BY_ASSET = (
    ("asset_checklists", "asset_id", None),
    ("fundamental_analysis", "asset_id", None),
    ("news_synthesis", "asset_id", None),
    ("news_consolidated", "asset_id", None),
    ("trading_list", "asset_id", "source = 'watchlist'"),
)


def _in_latest_screening(cur, ticker: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM screening_results
        WHERE upper(ticker) = %s AND run_at = (SELECT MAX(run_at) FROM screening_results)
        LIMIT 1
        """,
        (ticker.upper(),),
    )
    return cur.fetchone() is not None


def remove_asset(conn, asset_id: str) -> Optional[dict]:
    """ВИДАЛИТИ актив зі списку обраних: рядок `watchlist_assets`, його
    чек-лист і всі ПОХІДНІ аналізи (фундаментальний аналіз, синтез
    ціна↔новини, консолідовані новини, рядки списку торгів). Одна
    транзакція: або все, або нічого. None — активу немає.

    Що НЕ видаляється, і чому:
    - сирі дані — ціни в `raw_observations`, 8-K, Form 4, тексти
      звітів, календар звітності (правило 6: сирі дані не видаляються
      ніколи; повернутий актив одразу матиме історію);
    - `decision_outcomes` — журнал того, що агент радив і чи мав рацію.
      Видалити його означало б переписати власну статистику влучань;
    - `news_analysis` — класифікація сирих статей, живе разом із
      `raw_news` 48 годин і прибирається сама.

    Для АКЦІЇ новини й тези звіту, записані під тикером ("SKYD"),
    прибираються лише якщо тикера немає в останньому скринінгу — інакше
    вони належать скринінгу, а не списку обраних.

    Доти `/watchlist_remove` лише ставив `enabled = false`, і актив
    лишався в чек-листах, аналізах і списку торгів (живий фідбек
    користувача 2026-10-22)."""
    asset = find_asset(conn, asset_id)
    if asset is None:
        return None

    removed: dict[str, int] = {}
    try:
        with conn.cursor() as cur:
            keys = [asset_id]
            ticker = (asset.get("ticker") or "").upper()
            if asset.get("asset_class") == "stock" and ticker and not _in_latest_screening(cur, ticker):
                keys.append(ticker)
                cur.execute("DELETE FROM filing_insights WHERE upper(ticker) = %s", (ticker,))
                removed["filing_insights"] = cur.rowcount

            for table, column, condition in _DERIVED_BY_ASSET:
                where = f"{column} = ANY(%s)" + (f" AND {condition}" if condition else "")
                cur.execute(f"DELETE FROM {table} WHERE {where}", (keys,))
                removed[table] = cur.rowcount

            cur.execute("DELETE FROM watchlist_assets WHERE asset_id = %s", (asset_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    logger.info("watchlist_assets: %s ВИДАЛЕНО разом із похідними: %s", asset_id, removed)
    return removed
