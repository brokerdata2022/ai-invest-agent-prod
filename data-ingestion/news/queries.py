"""
Побудова GDELT query-рядка per stream — чисте звуження джерела
(rule 1: жодної оцінки релевантності тут, тільки те, ЩО шукати).

watchlist (docs/watchlist.md, закритий список активів поза акціями
S&P 500) — build_watchlist_query(). Акції зі скринінгу — окрема
функція build_stocks_query(), окремий запит (не один об'єднаний з
build_watchlist_query()) — щоб не наближатись до невідомого офіційно
ліміту довжини/к-сті OR-термінів GDELT DOC API. Обидві пишуться в той
самий stream="watchlist" (це та сама категорія "вже відібрані
активи"), просто двома окремими викликами collect().

geopolitical — БЕЗ GDELT-запиту тут (рішення користувача, 2026-09-27):
курований список фраз (сanкції/конфлікти/торгові війни/центробанки/
вибори) структурно ловить лише наперед передбачені теми — велика
непередбачена подія (загроза вторгнення, військове нарощування,
стихійне лихо) не збігається дослівно з жодною фразою й просто
НІКОЛИ не потрапляє в raw_news, без жодного попередження (живо
виявлено 2026-09-27 на реальному прикладі). Замінено на широкий
набір незалежних official RSS-фідів (news/rss_feeds.py — той самий
механізм, що вже був для Fed/ECB/BOJ): кожне джерело публікує
НАЙШИРШУ свою стрічку (не звужену під тему), а
aggregate.py:cluster_articles()/source_count вирішує значущість через
кількість незалежних джерел, що підхопили ту саму історію — "якщо
новина правдива, вона буде опублікована всюди", а не вгадування
ключових слів наперед. Деталі — docs/decisions.md, 2026-09-27.

general — build_general_queries() (два окремі GDELT-запити, ліміт
довжини — див. коментар нижче), широкий фінансовий контекст (без
прив'язки до конкретного активу) — "чи взагалі щось відбувається на
ринку, що варте уваги" (докладніше docs/decisions.md, 2026-09-25
"news/ — обсяг..."). GDELT query тут лишається — на відміну від
geopolitical, це не "спіймати будь-яку непередбачену подію", а вузько
market-специфічні терміни (rally/selloff/earnings/IPO/M&A), для яких
курований список прийнятний.
"""

# Один пошуковий термін на актив — ТОЙ САМИЙ набір, що раніше був
# ЄДИНИМ джерелом істини (docs/watchlist.md, закрито 2026-09-25).
# 2026-10-03 (docs/decisions.md, "редагування watchlist через
# Telegram"): справжнє джерело істини тепер таблиця watchlist_assets
# (common/watchlist_db.py:fetch_terms(conn)) — цей словник лишається
# як ДЕФОЛТНИЙ фолбек build_watchlist_query() нижче (і фікстура тестів,
# що перевіряють код без БД), не як факт "watchlist закритий".
WATCHLIST_TERMS: dict[str, str] = {
    "xauusd": '"gold price"',
    "xagusd": '"silver price"',
    "wti_crude": '"WTI crude"',
    "brent_crude": '"Brent crude"',
    "coffee": '"coffee futures"',
    "natgas": '"natural gas price"',
    "eurusd": '"EUR/USD"',
    "usdjpy": '"USD/JPY"',
    "btc": "Bitcoin",
    "eth": "Ethereum",
    "sol": "Solana",
}


def build_watchlist_query(terms: dict[str, str] = None) -> str:
    """`terms` — asset_id → пошуковий термін; None (дефолт) падає на
    WATCHLIST_TERMS вище. Викликач, що хоче ЖИВИЙ (редагований через
    Telegram) список, передає `common/watchlist_db.py:fetch_terms(conn)`
    явно (run_collect_news.py) — ця функція лишається чистою, без БД
    (rule 1 CLAUDE.md), той самий принцип, що build_stocks_query()."""
    effective_terms = terms if terms is not None else WATCHLIST_TERMS
    return "(" + " OR ".join(effective_terms.values()) + ")"


# Внутрішні ідентифікатори активів watchlist-потоку — підказка для
# DeepSeek (analysis/news_analysis/), яким asset_id позначати статтю,
# якщо вона прямо про один з цих активів. Ті самі ключі, що й
# WATCHLIST_TERMS (одне джерело істини для "що таке watchlist" —
# СТАТИЧНИЙ фолбек, живий список — common/watchlist_db.py:fetch_asset_ids(conn)).
WATCHLIST_ASSET_IDS: list[str] = list(WATCHLIST_TERMS)


# Ручні заміни назви для тикерів, де офіційна назва з S&P 500
# constituents.csv ("Security") сама по собі — надто коротке й надто
# загальновживане англійське слово для GDELT full-text search.
# Підтверджено ДВОМА живими прогонами 2026-09-25: query з терміном
# "Uber" (сам по собі, у лапках) GDELT відхиляв як "The specified
# phrase is too short" — і на 41 символі, і на 175 символах (в іншій
# групі з 8 додатковими термінами) — тобто причина не в довжині
# ЗАГАЛЬНОГО query (інші короткі однослівні назви в тих самих групах,
# "Emcor"/"Wabtec"/"Casey's", проходили без проблем). GDELT сам
# документує відхилення "дуже коротких АБО дуже загальновживаних"
# слів навіть у лапках (blog.gdeltproject.org) — "uber" це звичайне
# англійське слово (підсилювач, "über"), а не тільки назва компанії,
# на відміну від "Emcor"/"Wabtec". Точний алгоритм "загальновживаності"
# GDELT не документує, тому фіксуємо конкретний підтверджений випадок
# явно (як власну назву компанії, а не вгадану трансформацію) — той
# самий підхід кураторства, що й WATCHLIST_TERMS/METRICS в проєкті.
STOCK_NAME_OVERRIDES: dict[str, str] = {
    "UBER": "Uber Technologies",
}


def build_stocks_query(ticker_names: dict[str, str]) -> str:
    """Query для тикерів, що пройшли скринінг (analysis/screening/).

    ticker_names — {тикер: назва компанії}; пошуковий термін — назва
    компанії (STOCK_NAME_OVERRIDES, якщо є, інакше як дано), якщо
    відома (менш неоднозначна за GDELT full-text search, ніж короткий
    тикер на кшталт "BX"/"COP", які збігаються зі звичайними словами),
    інакше сам тикер. Список тикерів свідомо НЕ читається тут з
    БД/screening — ticker_names передає викликач (analysis/, бо
    data-ingestion не повинен залежати від analysis/, rule 1
    CLAUDE.md) — ця функція лишається чистою й тестованою без
    БД/мережі.
    """
    if not ticker_names:
        raise ValueError("ticker_names не може бути порожнім")
    terms = []
    for ticker, name in ticker_names.items():
        resolved = STOCK_NAME_OVERRIDES.get(ticker, name)
        terms.append(f'"{resolved}"' if resolved else ticker)
    return "(" + " OR ".join(terms) + ")"


# MAX_QUERY_LEN — підтверджено живо: 294-символьний query (16 повних
# назв) GDELT відхилив як "too short or too long", 117-150 символів
# проходили без проблем (docs/decisions.md, 2026-09-25) — 150
# консервативний запас під підтверджений робочий розмір, точна межа
# офіційно не задокументована.
#
# MIN_QUERY_LEN — НЕ підтверджено як окрема причина (docs/decisions.md,
# 2026-09-25 "MIN_QUERY_LEN — спростовано як причина"): єдиний
# 41-символьний query, що падав, також містив "Uber" (STOCK_NAME_
# OVERRIDES нижче) — той самий query довжиною 175 символів (після
# об'єднання з іншою групою) падав з тим самим "too short", тобто
# справжня причина була термін "Uber", не довжина. Поріг лишається як
# захисна евристика (шкоди не завдає — об'єднаний "хвіст" все одно
# лишається значно коротшим за підтверджений збійний максимум 294),
# але не варто довіряти йому як задокументованому факту.
MAX_QUERY_LEN = 150
MIN_QUERY_LEN = 70


# Геополітичний контекст (санкції/конфлікти/центробанки тощо) більше
# НЕ курований GDELT-запит тут — замінено на широкі RSS-фіди
# (news/rss_feeds.py), докладніше в докстрінгу модуля вище й
# docs/decisions.md, 2026-09-27.

# Курований набір широких ринкових тем — не прив'язаних до
# конкретного активу з watchlist — загальний "чи щось значуще
# відбувається на ринку" контекст. Кожен термін — багатослівна фраза
# або акронім ("IPO"), не окреме коротке загальновживане слово (урок
# "Uber", docs/decisions.md, 2026-09-25/26).
#
# Розбито на ДВІ групи (2026-09-28, живий фідбек користувача: сьогодні
# ринок падав, а жодної новини про це в raw_news не зібралось —
# перевірено, справжня причина не фільтр, а те, що набір термінів
# нижче ніколи не покривав ані назви індексів (S&P 500/Dow/Nasdaq),
# ані звичайні дієслова падіння ("stocks plunge/tumble/sink" —
# заголовки рідко пишуть саме "selloff" як іменник)):
# - MOVEMENT — як заголовки описують РУХ ринку (і вгору, і вниз).
# - EVENTS — конкретні ринкові події/індекси.
# Об'єднаний запит (15 термінів) — 266 символів, перевищує підтверджений
# робочий лімім GDELT (150, MAX_QUERY_LEN нижче) — тому ДВА окремі
# GDELT-запити (build_general_queries()), не один.
GENERAL_TERMS_MOVEMENT: dict[str, str] = {
    "rally": '"stock market rally"',
    "selloff": '"market selloff"',
    "plunge": '"stocks plunge"',
    "tumble": '"stocks tumble"',
    "sink": '"stocks sink"',
    "crash": '"market crash"',
    "volatility": '"market volatility"',
}

GENERAL_TERMS_EVENTS: dict[str, str] = {
    "earnings": '"earnings season"',
    "ipo": "IPO",
    "ma_deal": '"merger acquisition"',
    "recession": '"recession fears"',
    "sp500": '"S&P 500"',
    "dow": '"Dow Jones"',
    "nasdaq": "NASDAQ",
    "wallstreet": '"Wall Street"',
}

# Об'єднання обох груп — єдине джерело істини для "усі general-терміни
# загалом" (тести/аудит), сам запит завжди будується ОКРЕМО з кожної
# групи (build_general_queries()), не з цього об'єднання.
GENERAL_TERMS: dict[str, str] = {**GENERAL_TERMS_MOVEMENT, **GENERAL_TERMS_EVENTS}


def build_general_queries() -> list[str]:
    """ДВА окремі GDELT-запити (не один — див. коментар вище про
    ліміт довжини). Виклик (run_collect_news.py) робить collect() для
    КОЖНОГО, обидва пишуться в той самий stream="general"."""
    return [
        "(" + " OR ".join(GENERAL_TERMS_MOVEMENT.values()) + ")",
        "(" + " OR ".join(GENERAL_TERMS_EVENTS.values()) + ")",
    ]


def batch_ticker_names(
    ticker_names: dict[str, str],
    max_query_len: int = MAX_QUERY_LEN,
    min_query_len: int = MIN_QUERY_LEN,
) -> list[dict[str, str]]:
    """Ділить ticker_names на групи такого розміру, щоб
    build_stocks_query() кожної групи лишався під max_query_len
    символів — довжина назв компаній дуже різна ("Uber" проти
    "Huntington Ingalls Industries"), тому групування за к-стю
    тикерів (фіксоване число на групу) було б або занадто
    консервативним, або все одно інколи перевищувало б ліміт.

    Жадібне заповнення до max_query_len може лишити замалий "хвіст"
    в останній групі (GDELT відхиляє й це, min_query_len вище) —
    такий хвіст приєднується до попередньої групи, а не лишається
    окремим приреченим на "too short" запитом."""
    if not ticker_names:
        return []

    batches: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for ticker, name in ticker_names.items():
        candidate = {**current, ticker: name}
        if current and len(build_stocks_query(candidate)) > max_query_len:
            batches.append(current)
            current = {ticker: name}
        else:
            current = candidate
    if current:
        batches.append(current)

    if len(batches) > 1 and len(build_stocks_query(batches[-1])) < min_query_len:
        batches[-2] = {**batches[-2], **batches[-1]}
        batches.pop()

    return batches
