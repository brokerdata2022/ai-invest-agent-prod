-- Базова схема БД для Фази 0.
-- Дотримується правила з CLAUDE.md: сирі дані ніколи не видаляються
-- і не перезаписуються (append-only), тому raw_observations має
-- унікальність по (source, metric_id, observed_at, revision), а не
-- просто (source, metric_id, observed_at).

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Метадані джерел даних (список наростає з кожним новим адаптером,
-- див. .claude/skills/add-data-source).
CREATE TABLE IF NOT EXISTS sources (
    name        TEXT PRIMARY KEY,
    category    TEXT NOT NULL,        -- macro | companies | quotes | crypto | forex | commodities | news
    source_type TEXT NOT NULL,        -- official_primary | aggregator | unofficial
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Сирі зібрані значення показників. Append-only: новий факт або нова
-- ревізія старого факту — це завжди INSERT, ніколи UPDATE/DELETE.
CREATE TABLE IF NOT EXISTS raw_observations (
    id           BIGSERIAL,
    source       TEXT NOT NULL REFERENCES sources(name),
    metric_id    TEXT NOT NULL,       -- внутрішній стабільний id показника (не той, що в API джерела)
    value        NUMERIC NOT NULL,
    observed_at  DATE NOT NULL,       -- до якого періоду відноситься значення
    fetched_at   TIMESTAMPTZ NOT NULL,-- коли ми фактично його забрали
    revision     INTEGER NOT NULL DEFAULT 1,
    raw_payload  JSONB,               -- необроблена відповідь джерела для цього запису (аудит)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, observed_at),
    UNIQUE (source, metric_id, observed_at, revision)
);

-- TimescaleDB hypertable по observed_at (до якого періоду відноситься
-- значення), а не по fetched_at. Причина: TimescaleDB вимагає, щоб
-- будь-який унікальний індекс на hypertable (у т.ч. PRIMARY KEY і наш
-- UNIQUE) обов'язково включав колонку партиціювання. observed_at і так
-- частина бізнес-ключа (source, metric_id, observed_at, revision) —
-- тож підходить природно, без штучного розширення унікального
-- обмеження зайвою колонкою. (Партиціювання по fetched_at виглядало
-- логічним на перший погляд, але ламало створення hypertable — див.
-- docs/decisions.md.)
SELECT create_hypertable(
    'raw_observations', 'observed_at',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_raw_observations_lookup
    ON raw_observations (source, metric_id, observed_at DESC, revision DESC);

-- Лог релізів — заготовка під Фазу 3 (monitoring), щоб схема БД не
-- мінялась, коли дійдемо до моніторингу календаря.
CREATE TABLE IF NOT EXISTS release_log (
    id            BIGSERIAL PRIMARY KEY,
    source        TEXT NOT NULL REFERENCES sources(name),
    metric_id     TEXT NOT NULL,
    scheduled_at  TIMESTAMPTZ,
    detected_at   TIMESTAMPTZ,
    impact_level  TEXT,               -- high | medium | low
    status        TEXT NOT NULL DEFAULT 'pending', -- pending | detected | processed
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Додано 2026-09-26 (monitoring/economic_calendar.py): очікуване
-- ринком значення показника (форекс-календар дає "forecast" разом з
-- датою/часом релізу) — TEXT, бо джерело віддає мішані одиниці
-- ("0.6%", "-258B", "615K"), парсинг у число — робота analysis/, не
-- цього шару (rule 1, CLAUDE.md). ALTER, не тільки CREATE — таблиця
-- вже існувала й мала дані до цієї зміни, ALTER TABLE ADD COLUMN
-- IF NOT EXISTS — ідемпотентно, безпечно перезастосовувати
-- (apply_schema.py docstring).
ALTER TABLE release_log ADD COLUMN IF NOT EXISTS expected_value TEXT;

-- Сирі новини (окремо від raw_observations — там value NUMERIC,
-- новина текстова, туди не лягає, докладніше docs/decisions.md
-- 2026-09-25 "news/ — обсяг, межа шарів і схема БД"). Дедуп по
-- (source, external_id), append-only (без UPDATE/DELETE, rule 6).
-- НЕ hypertable: тут немає revision/DISTINCT ON-патерну, що
-- виправдовував partitioning для raw_observations.
CREATE TABLE IF NOT EXISTS raw_news (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT NOT NULL REFERENCES sources(name),
    external_id  TEXT NOT NULL,       -- дедуп-ключ джерела (URL/guid)
    stream       TEXT NOT NULL,       -- watchlist | general | geopolitical
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,-- коли статтю опубліковано (аналог observed_at)
    fetched_at   TIMESTAMPTZ NOT NULL,-- коли ми її фактично забрали
    raw_payload  JSONB,               -- необроблена відповідь джерела для цього запису (аудит)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_news_stream_published
    ON raw_news (stream, published_at DESC);

-- Лог кожного LLM-виклику (rule 5 з CLAUDE.md: промпт + відповідь +
-- timestamp + джерело — обов'язково для аудиту постфактум).
CREATE TABLE IF NOT EXISTS llm_call_log (
    id          BIGSERIAL PRIMARY KEY,
    provider    TEXT NOT NULL,        -- deepseek | anthropic
    purpose     TEXT NOT NULL,        -- напр. news_relevance_filter
    prompt      TEXT NOT NULL,
    response    TEXT NOT NULL,
    source_ref  TEXT,                 -- напр. id/URL статті, на яку був виклик
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Структурований висновок LLM-аналізу однієї новини (формат полів —
-- analysis/CLAUDE.md "Формат виходу LLM-аналізу"). asset_id — NULL
-- для geopolitical/general новин, що не стосуються конкретного активу.
CREATE TABLE IF NOT EXISTS news_analysis (
    id           BIGSERIAL PRIMARY KEY,
    raw_news_id  BIGINT NOT NULL REFERENCES raw_news(id),
    asset_id     TEXT,
    is_relevant  BOOLEAN NOT NULL,
    summary      TEXT,
    direction    TEXT,                -- up | down | neutral | unclear
    confidence   NUMERIC,
    reasoning    TEXT,
    llm_call_id  BIGINT REFERENCES llm_call_log(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Синтез "новини + ціна" по активу за вікно (analysis/news_analysis/
-- synthesize.py, docs/decisions.md "Ціль 1" — точки входу для
-- watchlist-пар: чи рух ціни пояснюється новинами, чи це шум/корекція).
-- Лише активи, для яких є ОБИДВА входи (новинний сигнал + ціна) — тому
-- price_* NOT NULL, на відміну від news_analysis.asset_id, який буває
-- NULL.
-- source_refs — список summaries новин, на які спирався висновок
-- (analysis/CLAUDE.md "Формат виходу LLM-аналізу"); AssetSignal не несе
-- URL на цьому рівні агрегації, summaries — найближчий доступний бекінг.
-- Один результат на актив НА ДЕНЬ (idx нижче + ON CONFLICT UPDATE у
-- save_synthesis(), не чистий append-only): повторний прогін того
-- самого дня (ручний тест, ретрай після збою) оновлює вже наявний
-- рядок замість дублювання — живо виявлено 2026-09-27, ручні тестові
-- прогони синтезу того самого активу того самого дня плодили по 3-5
-- рядків без жодної нової інформації.
CREATE TABLE IF NOT EXISTS news_synthesis (
    id                 BIGSERIAL PRIMARY KEY,
    asset_id           TEXT NOT NULL,
    window_days        INTEGER NOT NULL,
    cluster_count      INTEGER NOT NULL,
    net_lean           INTEGER NOT NULL,
    price_pct_change   NUMERIC NOT NULL,
    price_start_date   DATE NOT NULL,
    price_end_date     DATE NOT NULL,
    summary            TEXT NOT NULL,
    direction          TEXT NOT NULL,     -- up | down | neutral | unclear
    confidence         NUMERIC NOT NULL,
    reasoning          TEXT NOT NULL,
    source_refs        JSONB NOT NULL,
    llm_call_id        BIGINT REFERENCES llm_call_log(id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_news_synthesis_asset_per_day
    ON news_synthesis (asset_id, ((created_at AT TIME ZONE 'UTC')::date));

-- Синтез глобального контексту (analysis/news_analysis/synthesize_market.py,
-- docs/decisions.md "Ціль 4" — risk-on/risk-off стан ринку) —
-- geopolitical/general новини не прив'язані до активу (asset_id завжди
-- NULL), тому окрема від news_synthesis форма: без asset_id/price_*,
-- замість них macro_context (знімок ключових ставок/дохідностей на
-- момент синтезу, для аудиту) і source_refs зі списком урахованих
-- кластерів (title+source_count, не окремих статей).
-- Один результат НА ДЕНЬ (idx нижче + ON CONFLICT UPDATE у
-- save_market_synthesis()) — той самий принцип, що news_synthesis вище.
CREATE TABLE IF NOT EXISTS market_synthesis (
    id             BIGSERIAL PRIMARY KEY,
    window_days    INTEGER NOT NULL,
    cluster_count  INTEGER NOT NULL,
    macro_context  JSONB NOT NULL,
    summary        TEXT NOT NULL,
    direction      TEXT NOT NULL,     -- up (risk-on) | down (risk-off) | neutral | unclear
    confidence     NUMERIC NOT NULL,
    reasoning      TEXT NOT NULL,
    source_refs    JSONB NOT NULL,
    llm_call_id    BIGINT REFERENCES llm_call_log(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Унікальність market_synthesis перенесена на (дата, СЕСІЯ) —
-- `idx_market_synthesis_per_session` нижче (2026-10-04, три сесійні
-- синтези на добу). Оригінальний `idx_market_synthesis_per_day`
-- (UNIQUE лише на даті) ВИДАЛЕНО звідси, а не заглушено DROP-ом
-- нижче: schema.sql виконується зверху вниз, тож CREATE тут
-- відтворював би індекс при кожному застосуванні — і падав би з
-- UniqueViolation, щойно за добу з'явиться більше однієї сесії
-- (живий збій 2026-10-04).

-- Кандидати-новачки, знайдені LLM у general-потоці новин
-- (analysis/news_analysis/discover_candidates.py, docs/decisions.md
-- "Ціль 3") — активи, яких ще немає в S&P 500 universe. Журнал
-- відкриттів (append-only МІЖ днями); "поточний список" (10
-- найновіших унікальних тикерів) — DISTINCT ON (ticker) ORDER BY
-- discovered_at DESC. Кожен рядок тут ВЖЕ пройшов верифікацію
-- торгованості через Twelve Data (verify_tradable()) — LLM лише
-- пропонує, підтвердження факту, що тикер реальний і ліквідний,
-- завжди детерміноване, не на слово LLM.
-- Один рядок на тикер НА ДЕНЬ (idx нижче + ON CONFLICT UPDATE у
-- save_candidate()) — той самий принцип, що news_synthesis/
-- market_synthesis вище: повторне виявлення того самого тикера того
-- самого дня оновлює рядок, не дублює.
CREATE TABLE IF NOT EXISTS candidate_assets (
    id             BIGSERIAL PRIMARY KEY,
    ticker         TEXT NOT NULL,
    company_name   TEXT NOT NULL,
    reasoning      TEXT NOT NULL,
    source_refs    JSONB NOT NULL,
    llm_call_id    BIGINT REFERENCES llm_call_log(id),
    discovered_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_assets_ticker_per_day
    ON candidate_assets (ticker, ((discovered_at AT TIME ZONE 'UTC')::date));

-- Зареєстровані джерела.
INSERT INTO sources (name, category, source_type, notes) VALUES
    ('fred', 'macro', 'official_primary', 'Federal Reserve Economic Data (US)'),
    ('ecb', 'macro', 'official_primary', 'ECB Data Portal (колишній SDW), єврозона'),
    ('boj', 'macro', 'official_primary', 'Bank of Japan Time-Series Data Search API, Японія'),
    ('estat', 'macro', 'official_primary', 'e-Stat (政府統計の総合窓口) API v3.0, Японія'),
    ('fed_speeches_rss', 'news', 'official_primary', 'Federal Reserve — RSS промов членів Правління. Доданий 2026-10-13 для повного синтезу ставки: fed_rss це press_all.xml, тобто прес-релізи БЕЗ промов, а рішення формується в публічних виступах задовго до засідання'),
    ('fed_testimony_rss', 'news', 'official_primary', 'Federal Reserve — RSS свідчень у Конгресі (Semiannual Monetary Policy Report тощо), той самий привід, що fed_speeches_rss'),
    ('bls', 'macro', 'official_primary', 'Bureau of Labor Statistics public API v2 — ПЕРШОДЖЕРЕЛО тих рядів, які FRED лише передруковує (CPI/Core CPI/безробіття/зайнятість). Доданий 2026-10-13 як ДРУГЕ джерело для звірки макро (common/consensus.py): після кейсу з Brent, де FRED помилявся на 33%, "з макро, напевно, все гаразд" стало припущенням, а не знанням. Усі ряди СЕЗОННО ЗГЛАДЖЕНІ (CUSR/LNS/CES) — щоб бути парою до FRED; незгладжений CUUR дає інше число на тих самих даних. Без ключа 25 запитів/добу'),
    ('yahoo_chart', 'quotes', 'aggregator', 'Yahoo Finance chart API (query1, недокументований) — ДРУГЕ незалежне джерело ціни watchlist-товарів/форексу/крипти + ІСТОРІЯ (127-184 денні точки проти 6-7 у tradingeconomics). Доданий 2026-10-12 після того, як звірка показала: FRED дає Brent 135.51 проти справжніх 102.25 (викид 33%), а кава у FRED — з липня. Свідомо перетинається по metric_id з fred/tradingeconomics, щоб джерела можна було ЗВІРЯТИ (common/consensus.py)'),
    ('twelvedata', 'quotes', 'aggregator', 'Twelve Data — щоденні ціна/обсяг акцій, безкоштовна реєстрація без капчі (Stooq відкинуто через бот-захист, Alpaca — гео-блок; docs/decisions.md 2026-09-20)'),
    ('sec_edgar', 'companies', 'official_primary', 'SEC EDGAR XBRL (companyconcept) — фундаментальні факти компаній, без ключа, обов''язковий User-Agent'),
    ('gdelt', 'news', 'aggregator', 'GDELT DOC 2.0 API — глобальний агрегатор новин, query-фільтр на рівні запиту (watchlist/general потоки — geopolitical перенесено на широкі RSS-фіди, docs/decisions.md 2026-09-27), без ключа'),
    ('fed_rss', 'news', 'official_primary', 'Federal Reserve — офіційний RSS усіх прес-релізів, без ключа'),
    ('ecb_rss', 'news', 'official_primary', 'ECB — офіційний RSS прес-релізів/промов/прес-конференцій, без ключа'),
    ('boj_rss', 'news', 'official_primary', 'Bank of Japan — офіційний RSS новин (whatsnew), без ключа'),
    ('bbc_rss', 'news', 'aggregator', 'BBC News — головний RSS (усі категорії, не звужений), без ключа'),
    ('aljazeera_rss', 'news', 'aggregator', 'Al Jazeera — RSS "all" (усі категорії), без ключа'),
    ('guardian_rss', 'news', 'aggregator', 'The Guardian — RSS World, без ключа'),
    ('npr_rss', 'news', 'aggregator', 'NPR — головний RSS (Top Stories), без ключа'),
    ('skynews_rss', 'news', 'aggregator', 'Sky News — RSS World, без ключа'),
    ('dw_rss', 'news', 'aggregator', 'Deutsche Welle — RSS "all" (англомовний), без ключа'),
    ('cnbc_markets_rss', 'news', 'aggregator', 'CNBC — RSS Markets, без ключа (потік general, 2026-10-05)'),
    ('marketwatch_rss', 'news', 'aggregator', 'MarketWatch — RSS Top Stories (Dow Jones), без ключа (потік general)'),
    ('investing_rss', 'news', 'aggregator', 'Investing.com — RSS news, без ключа (потік general)'),
    ('bbc_business_rss', 'news', 'aggregator', 'BBC Business — RSS, без ключа (потік general)'),
    ('guardian_business_rss', 'news', 'aggregator', 'The Guardian Business — RSS, без ключа (потік general)'),
    ('npr_business_rss', 'news', 'aggregator', 'NPR Business — RSS, без ключа (потік general)'),
    ('nasdaq_markets_rss', 'news', 'aggregator', 'Nasdaq — RSS Markets, без ключа (потік general)'),
    ('ft_rss', 'news', 'aggregator', 'Financial Times — RSS home (лише title+url, тіло за пейволлом; потік general)'),
    ('binance', 'crypto', 'official_primary', 'Binance public API — щоденні OHLCV-свічки (klines) BTC/ETH/SOL проти USDT, без ключа'),
    ('coingecko', 'crypto', 'aggregator', 'CoinGecko — щоденний market cap BTC/ETH/SOL (агрегатор по біржах), без ключа'),
    ('binance_futures', 'crypto', 'official_primary', 'Binance USDⓈ-M Futures public API — bulk-знімок ринку (обсяг/funding rate) для крипто-скринінгу лонг/шорт/спостереження, без ключа (docs/decisions.md 2026-09-27)'),
    ('bybit_futures', 'crypto', 'official_primary', 'Bybit v5 public API (linear perpetual) — bulk-знімок ринку (обсяг/funding rate/Open Interest, усе в одному запиті), без ключа'),
    ('okx_futures', 'crypto', 'official_primary', 'OKX v5 public API (SWAP) — bulk-знімок ринку (обсяг/Open Interest), без ключа'),
    ('web_crosscheck', 'commodities', 'unofficial', 'ОСТАННІЙ резерв (2026-10-04, rule 7 CLAUDE.md) — лише коли для watchlist-активу НЕМАЄ жодного автоматизованого джерела (ні структурований API, ні скрапінг-адаптер типу tradingeconomics_adapter.py). Значення — ручний/LLM-веб-пошук, звірений МІНІМУМ на 2 сайтах (common/manual_observation.py), raw_payload містить source_refs. НЕ в розкладі APScheduler — вимагає LLM-сесію на КОЖНЕ оновлення (живий урок: coffee/wti_crude/brent_crude/natgas протрималися тут лише кілька годин цієї сесії, доки не знайшовся справжній автоматизований tradingeconomics-скрапінг, на який їх і перемкнуто — UPDATE нижче).'),
    ('tradingeconomics', 'commodities', 'unofficial', 'Скрапінг tradingeconomics.com/commodity/<slug> (2026-10-04, commodities/tradingeconomics_adapter.py) — ціна+дата парситься з <meta name="description"> (живо підтверджено стабільний формат на 5 товарах, звичайний requests.get(), жодного JS/бот-захисту, на відміну від Stooq, відкинутого 2026-09-20). Джерело для watchlist-товарів, яким основне джерело не дає надійної щоденної ціни: coffee (FRED — місячна серія), wti_crude/brent_crude/natgas (FRED живо застряг на кілька днів), xauusd (ІНША причина — Twelve Data forex-OTC ревізії того самого дня розходились на 80+ пунктів заднім числом, docs/decisions.md). На розкладі _watchlist_prices — автоматично, без людини, на відміну від web_crosscheck вище.')
ON CONFLICT (name) DO NOTHING;

-- Порівняння факту з ринковим очікуванням для одного циклу релізу
-- (release_log.status: detected -> processed, analysis/expectations/).
-- Append-only, по одному рядку на release_log_id — той самий реліз
-- не порівнюється двічі. actual_value/expected_value_parsed завжди в
-- ОДНАКОВИХ одиницях (comparison_method визначає, як вони приведені
-- до спільного вигляду — деталі docs/decisions.md), expected_value_raw
-- лишає оригінальний текст ForexFactory для аудиту.
CREATE TABLE IF NOT EXISTS expectation_comparisons (
    id                     BIGSERIAL PRIMARY KEY,
    release_log_id         BIGINT NOT NULL UNIQUE REFERENCES release_log(id),
    source                 TEXT NOT NULL,
    metric_id              TEXT NOT NULL,
    observed_at            DATE NOT NULL,
    actual_value           NUMERIC NOT NULL,
    expected_value_raw     TEXT NOT NULL,
    expected_value_parsed  NUMERIC NOT NULL,
    surprise               NUMERIC NOT NULL,
    surprise_pct           NUMERIC,
    comparison_method      TEXT NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Додано 2026-09-27: NULL = ще не надіслано в Telegram.
-- expectations_notify.py проставляє тут now() одразу після успішної
-- відправки — без цього notify_expectations (розклад — кожні 15 хв)
-- повторно надсилав би ті самі "останні 5" рядків щоцикл, поки не
-- набіжить новий реліз (живий баг, знайдений під час розгортання з
-- нуля — docs/decisions.md, 2026-09-27). ALTER, не тільки CREATE —
-- та сама причина, що вище: таблиця вже існувала й мала дані.
ALTER TABLE expectation_comparisons ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;

-- LLM-синтез причинного висновку поверх уже готового детермінованого
-- сюрпризу (analysis/expectations/synthesize.py, docs/decisions.md
-- "наступний крок Фази 2") — "вийшло X, очікувалось Y, це означає Z"
-- (PLAN.md, критерій завершення Фази 2). Один рядок НА comparison_id
-- (не append-only, як news_synthesis/market_synthesis вище) — той
-- самий release_log-цикл не синтезується двічі, і expectation_comparisons
-- сам уже UNIQUE по release_log_id.
CREATE TABLE IF NOT EXISTS expectation_synthesis (
    id             BIGSERIAL PRIMARY KEY,
    comparison_id  BIGINT NOT NULL UNIQUE REFERENCES expectation_comparisons(id),
    summary        TEXT NOT NULL,
    direction      TEXT NOT NULL,     -- up | down | neutral | unclear
    confidence     NUMERIC NOT NULL,
    reasoning      TEXT NOT NULL,
    source_refs    JSONB NOT NULL,
    llm_call_id    BIGINT REFERENCES llm_call_log(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Прогнози трендової моделі (analysis/forecasting/, PLAN.md Фаза 2
-- "базовий механізм прогнозування"). Один рядок на (source, metric_id,
-- method, based_on_observed_at) — той самий принцип UPSERT, що
-- news_synthesis: повторний прогін на тих самих вхідних даних оновлює
-- рядок, не дублює. based_on_observed_at — дата останньої фактичної
-- точки, з якої екстрапольовано (для аудиту "на чому базувався прогноз").
CREATE TABLE IF NOT EXISTS metric_forecasts (
    id                    BIGSERIAL PRIMARY KEY,
    source                TEXT NOT NULL,
    metric_id             TEXT NOT NULL,
    method                TEXT NOT NULL,      -- напр. linear_trend
    based_on_observed_at  DATE NOT NULL,
    periods_ahead         INTEGER NOT NULL DEFAULT 1,
    forecast_value        NUMERIC NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- "На що звернути увагу при виході" (2026-10-13, постановка
    -- користувача 2026-10-11: принцип агента цього вимагав, а в
    -- контракті прогнозу поля не було — було ЧИСЛО й обґрунтування,
    -- але не те, ЩО саме перевіряти в релізі). NULL для naive-методів:
    -- вони не викликають LLM узагалі.
    watch_on_release TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_metric_forecasts_unique
    ON metric_forecasts (source, metric_id, method, based_on_observed_at);

-- Результати кожного прогону composite_score.py (analysis/screening/) —
-- раніше тільки друкувались, ніде не зберігались (docs/decisions.md), тому
-- ніщо не знало "які тикери зараз пройшли скринінг" (потрібне
-- analysis/news_analysis/run_news_analysis.py для tracked_assets акцій,
-- docs/decisions.md "Ціль 2"). Append-only, як решта проєкту — усі
-- рядки ОДНОГО прогону мають той самий run_at, "поточний" скринінг =
-- MAX(run_at). Порожній прогін (ніхто не пройшов) нічого не вставляє —
-- свідомо: не затирати вчорашній валідний список нульовим/збійним прогоном.
CREATE TABLE IF NOT EXISTS screening_results (
    id                 BIGSERIAL PRIMARY KEY,
    ticker             TEXT NOT NULL,
    score              NUMERIC NOT NULL,
    revenue_growth     NUMERIC,
    eps_growth         NUMERIC,
    pe                 NUMERIC,
    avg_dollar_volume  NUMERIC,
    run_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_screening_results_run_at ON screening_results (run_at);

-- Views для читабельного перегляду. raw_observations лишається
-- append-only джерелом істини (жодних UPDATE/DELETE) — views тільки
-- читають і не змінюють дані, це суто зручність перегляду, не заміна
-- окремих таблиць (docs/decisions.md, 2026-09-21).

-- Часовий ряд одного показника без "шуму" старих ревізій — для
-- кожної (source, metric_id, observed_at) лишається тільки запис з
-- найвищим revision (останнє відоме значення на цю дату).
CREATE OR REPLACE VIEW v_observations_latest_revision AS
SELECT DISTINCT ON (source, metric_id, observed_at)
    source, metric_id, value, observed_at, fetched_at, revision
FROM raw_observations
ORDER BY source, metric_id, observed_at, revision DESC;

-- Поточне значення кожного показника одним рядком (найсвіжіша дата,
-- найвища ревізія на неї) — "що зараз", а не весь часовий ряд.
-- Приклад: SELECT * FROM v_current_values WHERE metric_id LIKE 'aapl%';
CREATE OR REPLACE VIEW v_current_values AS
SELECT DISTINCT ON (source, metric_id)
    source, metric_id, value, observed_at, fetched_at, revision
FROM raw_observations
ORDER BY source, metric_id, observed_at DESC, revision DESC;

-- Крипто-скринінг лонг/шорт/спостереження (analysis/crypto_screening/,
-- PLAN.md Фаза 4, докладніше docs/decisions.md 2026-09-27) — на відміну
-- від screening_results (акції, знімок ОДНОГО прогону), тут МУТАБЕЛЬНИЙ
-- стан, що живе МІЖ прогонами: денний скан (run_screening.py) заводить
-- 'candidate' при виявленні пампу; погодинний моніторинг
-- (monitor_candidates.py) оновлює той самий рядок і переводить статус
-- (candidate -> short/watch, або candidate/watch -> closed, коли памп
-- вичерпався). last_oi_usd — знімок з ПОПЕРЕДНЬОЇ погодинної
-- перевірки, потрібен для рахування дельти "з минулого разу" (не з
-- фіксованого вікна годин — користувач, 2026-09-27: "тримати, поки
-- дані відповідають", без таймера). last_quote_volume НЕ створюємо
-- тут (2026-10-03) — сплеск обсягу тепер рахується з 4г-Binance
-- klines "на льоту" (compute_monitoring_indicators), не зі
-- збереженого 24h-знімка.
CREATE TABLE IF NOT EXISTS crypto_screening_candidates (
    id                     BIGSERIAL PRIMARY KEY,
    symbol                 TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'candidate',  -- candidate | short | watch | closed
    detected_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked_at        TIMESTAMPTZ,
    closed_at              TIMESTAMPTZ,
    pump_pct_at_detection  NUMERIC NOT NULL,
    last_pump_pct          NUMERIC,
    last_funding_rate      NUMERIC,
    last_oi_usd            NUMERIC,
    reason                 TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Лише ОДИН активний (не closed) рядок на symbol одночасно — новий
-- памп тієї самої монети через тижні заводить НОВИЙ рядок (історія
-- попередніх циклів лишається, rule 6 у дусі — не raw-дані, але той
-- самий принцип "не губити минуле").
CREATE UNIQUE INDEX IF NOT EXISTS idx_crypto_candidates_active_symbol
    ON crypto_screening_candidates (symbol) WHERE status != 'closed';

-- Дедуп сповіщень (2026-09-28, критичний фікс — живий фідбек
-- користувача: news_notify.py надсилав ІДЕНТИЧНІ ранкове й вечірнє
-- повідомлення, коли за день з'являлось мало нового). Той самий
-- принцип, що вже застосований у expectation_comparisons.notified_at
-- вище (2026-09-27): NULL = ще не надіслано, reporting-скрипт
-- проставляє now() одразу після успішної відправки. ALTER, не тільки
-- CREATE — усі чотири таблиці вже існували й мали дані до цієї зміни.
ALTER TABLE news_analysis ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;
ALTER TABLE news_synthesis ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;
ALTER TABLE market_synthesis ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;
ALTER TABLE candidate_assets ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;

-- Консолідований новинний дайджест (2026-09-28, живий фідбек
-- користувача: news_notify.py слав по окремій статті — 3 албанських
-- сайти з тим самим текстом про Bitcoin 84000 йшли ТРЬОМА окремими
-- повідомленнями, мовою оригіналу). analysis/news_analysis/consolidate.py
-- бере СИРІ статті (не news_analysis — навмисно, щоб DeepSeek сам
-- відсіював нерелевантне, а не покладався на relevance_filter.py) за
-- вікно й ОДНИМ LLM-викликом на потік: фільтрує шум, об'єднує статті
-- про ОДНУ подію (навіть різними мовами/сайтами) в один запис,
-- перекладає українською. source_raw_news_ids/source_urls — які
-- сирі статті об'єднано (аудит); джерела приєднує КОД за індексами,
-- які повернув DeepSeek, не сам DeepSeek (той самий принцип, що
-- relevance_filter.py — ризик галюцинації URL).
CREATE TABLE IF NOT EXISTS news_consolidated (
    id                  BIGSERIAL PRIMARY KEY,
    stream              TEXT NOT NULL,
    asset_id            TEXT,
    summary             TEXT NOT NULL,
    direction           TEXT NOT NULL,     -- up | down | neutral | unclear
    confidence          NUMERIC NOT NULL,
    reasoning           TEXT NOT NULL,
    source_count        INTEGER NOT NULL,  -- скільки сирих статей об'єднано
    source_raw_news_ids JSONB NOT NULL,
    source_urls         JSONB NOT NULL,
    llm_call_id         BIGINT REFERENCES llm_call_log(id),
    notified_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_news_consolidated_stream_created
    ON news_consolidated (stream, created_at DESC);

-- Яку сиру статтю вже враховано в якомусь прогоні консолідації —
-- щоб той самий прогін (кілька разів на добу) не пережовував ту саму
-- статтю знову й знову. NULL = ще не оброблено.
ALTER TABLE raw_news ADD COLUMN IF NOT EXISTS consolidated_at TIMESTAMPTZ;

-- Свіжість raw_news (2026-09-28, рішення користувача) — СВІДОМИЙ
-- ВИНЯТОК із critical rule 6 кореневого CLAUDE.md ("ніколи не
-- видаляти сирі дані"): обґрунтування правила ("історичні дані — те,
-- на чому тримається якість МАЙБУТНІХ ПРОГНОЗІВ") стосується
-- макропоказників (raw_observations), не новинних статей — стаття
-- тижневої давності не покращує прогноз, лише захаращує "важливі
-- новини" застарілим контентом. Збір (common/news_db.py:
-- MAX_ARTICLE_AGE_HOURS=24) і зберігання (RETENTION_HOURS=48,
-- prune_stale_news(), orchestration-джоба prune_raw_news) тепер МАЮТЬ
-- часову межу. raw_observations (макро/акції/крипта) ЛИШАЄТЬСЯ
-- append-only — це правило їх не торкається.
--
-- ON DELETE CASCADE потрібен: news_analysis.raw_news_id — єдиний
-- реальний FK на raw_news(id) (news_consolidated.source_raw_news_ids —
-- JSONB-масив, не FK, видалення raw_news його не зачіпає). Без CASCADE
-- prune_stale_news() падав би з foreign key violation на будь-якій
-- уже проаналізованій статті. DROP+ADD — ALTER ідемпотентний
-- (apply_schema.py), як решта міграцій цього файлу.
ALTER TABLE news_analysis DROP CONSTRAINT IF EXISTS news_analysis_raw_news_id_fkey;
ALTER TABLE news_analysis ADD CONSTRAINT news_analysis_raw_news_id_fkey
    FOREIGN KEY (raw_news_id) REFERENCES raw_news(id) ON DELETE CASCADE;

-- Причинна атрибуція ціна/новини (news_synthesis) була примітивною
-- (живий фідбек користувача, 2026-10-02): "ціна -5%, новини +4" саме
-- собою не пояснює нічого й не дає гіпотези, яку можна перевірити.
-- confirmation_factors — окреме поле LLM-висновку (synthesize.py:
-- PriceNewsSynthesisResult) поруч із summary: конкретні фактори/дані,
-- які підтвердили б чи спростували гіпотезу тренд/корекція (НЕ
-- прогноз ціни і НЕ торгова рекомендація — описова характеристика,
-- analysis/CLAUDE.md "Заборонені формулювання"). NULL дозволено — рядки
-- до цієї зміни лишаються без нового поля, не зникають.
ALTER TABLE news_synthesis ADD COLUMN IF NOT EXISTS confirmation_factors TEXT;

-- Комплексний міжактивний вплив релізу (2026-10-02, живий фідбек
-- користувача): попередній синтез давав напрямок лише для ОДНОГО
-- пов'язаного активу/валюти — користувач просив розбір впливу на
-- ставку/економіку/валюту/акції/крипту/золото/будь-який інший актив,
-- і ЛИШЕ для категорій, які реліз РЕАЛЬНО зачіпає (без "не впливає"-
-- заповнювачів). asset_impacts — масив {category, assets, direction,
-- explanation} від LLM (analysis/expectations/synthesize.py); NULL/[]
-- дозволено — рядки до цієї зміни лишаються без нового поля.
ALTER TABLE expectation_synthesis ADD COLUMN IF NOT EXISTS asset_impacts JSONB;

-- impact_notified_at (2026-10-02, рішення користувача того самого дня:
-- "сюрприз лишається сюрпризом, а широкий аналіз ринку — це новий
-- [звіт]") — ОКРЕМИЙ дедуп від expectation_comparisons.notified_at
-- (яким керує expectations_notify.py, короткий сюрприз-звіт).
-- reporting/release_impact_notify.py (новий, окремий Telegram-звіт із
-- розбором asset_impacts вище) проставляє цю колонку одразу після
-- успішної відправки — той самий принцип NULL = ще не надіслано, що
-- всюди в проєкті (reporting/CLAUDE.md "Дедуплікація сповіщень").
ALTER TABLE expectation_synthesis ADD COLUMN IF NOT EXISTS impact_notified_at TIMESTAMPTZ;

-- Heartbeat планувальника (2026-10-02, живий випадок користувача:
-- реліз NFP/Unemployment о 15:30 опрацьовано лише о 17:57 —
-- розслідування показало, що сам конвеєр check_releases→...→
-- notify_expectations займає ~15 хв, а причина затримки була в тому,
-- що контейнер scheduler був недоступний ~11 годин (докер/хост не
-- піднятий) і ніхто про це не дізнався, поки звіт не прийшов із
-- запізненням. Один рядок (id=1): jobs.py:_scheduler_heartbeat
-- оновлює last_tick_at кожні 5 хв; orchestration/main.py звіряє
-- розрив при старті процесу й шле один Telegram-алерт, якщо простій
-- був ненормально довгим (docs/decisions.md, P0 "Heartbeat
-- планувальника" — перший практичний крок).
CREATE TABLE IF NOT EXISTS scheduler_heartbeat (
    id           SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_tick_at TIMESTAMPTZ NOT NULL
);

-- notified_at для screening_results (2026-10-02, docs/decisions.md
-- розділ 3а №1: "скринінг рахується щодня, але жодного reporting-скрипта
-- для нього немає"): reporting/screening_notify.py — той самий принцип
-- NULL = ще не надіслано, що всюди в проєкті. Дедуп тут не по id рядків
-- (композит рахується на весь S&P-прохід одразу), а по run_at: нотифай
-- позначає весь run_at одним UPDATE, щоб той самий щоденний прогін не
-- надсилався повторно, якщо notify-джоба випадково спрацює двічі.
ALTER TABLE screening_results ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;

-- company_name/price_change_24h_pct для screening_results (2026-10-02,
-- живий фідбек користувача: вивід score/revenue/eps/pe/avg_dollar_volume
-- "страшний" — реальна потреба читача Telegram-звіту: тикер, назва
-- компанії, зміна ціни за 24г, не сирі компоненти формули ранжування).
-- composite_score.py рахує й зберігає обидва поля ПІСЛЯ самого
-- ранжування (не впливають на score) — лише контекст для виводу.
-- company_name — з constituents.csv (collect_universe.py, той самий
-- підхід, що вже використовує discover_candidates.py); NULL, якщо
-- мережевий запит не вдався (screening_notify.py тоді показує тикер
-- замість назви, не падає).
ALTER TABLE screening_results ADD COLUMN IF NOT EXISTS company_name TEXT;
ALTER TABLE screening_results ADD COLUMN IF NOT EXISTS price_change_24h_pct NUMERIC;

-- price_date/volume_change_24h_pct (2026-10-02, живий фідбек
-- користувача: "на яку дату ці дані? чи є можливість добавити обсяг
-- торгів за 24 год у доларах?", того самого дня уточнено: "обсяг
-- потрібно в відсотках зміни за 24 години", не в доларах —
-- volume_change_24h_pct одразу в підсумковій формі, попередній варіант
-- у $ ніде не встиг використовуватись). price_date — дата ОСТАННЬОГО
-- закриття, на основі якого рахуються обидва %-поля (не run_at самого
-- скринінгу — можуть відрізнятись на кілька годин через час збору
-- котирувань). volume_change_24h_pct — % зміна обсягу торгів день-до-дня
-- (twelvedata), той самий стиль, що price_change_24h_pct.
ALTER TABLE screening_results ADD COLUMN IF NOT EXISTS price_date DATE;
ALTER TABLE screening_results ADD COLUMN IF NOT EXISTS volume_change_24h_pct NUMERIC;
-- volume_24h_usd — попередня (тим самим днем, кілька хвилин) версія
-- цього поля в доларах, жодного живого коду чи звіту вже не читає її.
ALTER TABLE screening_results DROP COLUMN IF EXISTS volume_24h_usd;

-- notified_at для crypto_screening_candidates (2026-10-02, PLAN.md
-- Фаза 4 Крок 5: "Telegram-сповіщення про SHORT/WATCH, зараз лише
-- лог" — закрито для SHORT/WATCH тут; LONG-кандидати run_screening.py
-- й далі лише логуються, у цій таблиці не персистуються, окреме
-- рішення). На відміну від append-only таблиць вище, рядок тут
-- МУТАБЕЛЬНИЙ (monitor_candidates.py оновлює status щогодини) —
-- _candidates_db.py:update_candidate() скидає notified_at на NULL,
-- коли status РЕАЛЬНО змінився (напр. candidate→short), той самий
-- принцип, що вже застосований до news_synthesis/market_synthesis
-- (2026-09-29, "UPSERT не скидав notified_at — повторний результат
-- того самого дня мовчки губився") — інакше значущий перехід
-- лишився б непоміченим під уже проставленим notified_at.
ALTER TABLE crypto_screening_candidates ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;

-- LONG-кандидати крипто-скринінгу (2026-10-02, рішення користувача:
-- закрити LONG перед тестуванням notify-скриптів). На відміну від
-- crypto_screening_candidates вище (SHORT/WATCH, мутабельний стан —
-- памп, що з часом вичерпується, тож потребує погодинного
-- моніторингу статусу), LONG-сигнал — "сьогодні тренд підтверджений"
-- для вже Tier A-допущеного символу (run_screening.py, широкий денний
-- скан усього ринку) — append-only ЗНІМОК одного прогону, той самий
-- принцип, що screening_results (акції): один run_at на весь виклик,
-- notified_at для дедупу Telegram, "останній прогін" = MAX(run_at).
-- Раніше LONG лише логувався (logger.info), ніколи не йшов у БД.
CREATE TABLE IF NOT EXISTS crypto_long_candidates (
    id            BIGSERIAL PRIMARY KEY,
    symbol        TEXT NOT NULL,
    oi_change_pct NUMERIC,
    rsi_value     NUMERIC,
    funding_rate  NUMERIC,
    run_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    notified_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_crypto_long_candidates_run_at ON crypto_long_candidates (run_at);

-- last_price для crypto_screening_candidates (2026-10-03, живий кейс
-- користувача: MAGMAUSDT не підхоплений на SHORT, хоча реальний
-- розворот стався в межах години — pump_pct/volume_spike_pct, які
-- вже є, рахуються з 24-ГОДИННИХ rolling-показників самих бірж
-- (price24hPcnt/priceChangePercent), тож свіжий розворот В МЕЖАХ
-- ГОДИНИ розмивається добовим вікном і може лишатись непоміченим
-- кілька годин. last_price — ціна (Binance mark_price) на момент
-- ПОПЕРЕДНЬОЇ погодинної перевірки — monitor_candidates.py рахує
-- price_change_pct_1h = (поточна - last_price) / last_price, і це
-- йде в short_watch_screen.py як ДОДАТКОВИЙ (не єдиний) шлях
-- підтвердження розвороту, поруч із volume_spike_pct.
ALTER TABLE crypto_screening_candidates ADD COLUMN IF NOT EXISTS last_price NUMERIC;

-- last_quote_volume (2026-10-03): сплеск обсягу під час моніторингу
-- тепер рахується з 4г-Binance klines "на льоту"
-- (run_screening.py:compute_monitoring_indicators), не зі збереженого
-- 24h-знімка біржі — колонка ніде більше не читалась (ані для
-- рішення, ані у Telegram-виводі), прибрано, не лишено мертвою.

-- Ранковий огляд календаря релізів (analysis/calendar_outlook/,
-- docs/decisions.md 2026-10-03): у понеділок — огляд на весь тиждень
-- (scope='week'), в інший робочий день — лише на сьогодні
-- (scope='day'). release_log_ids — які 'pending'-рядки release_log
-- увійшли в огляд (для форматування деталей у Telegram, reporting/
-- не рахує нічого самостійно). Один рядок на (outlook_date, scope) —
-- idempotent: повторний прогін тієї самої доби (напр. ретрай
-- orchestration/runner.py) не дублює LLM-виклик.
CREATE TABLE IF NOT EXISTS calendar_outlook (
    id              BIGSERIAL PRIMARY KEY,
    outlook_date    DATE NOT NULL,
    scope           TEXT NOT NULL,       -- week | day
    release_log_ids BIGINT[] NOT NULL,
    direction       TEXT NOT NULL,       -- up | down | neutral | unclear
    confidence      NUMERIC NOT NULL,
    summary         TEXT NOT NULL,
    reasoning       TEXT NOT NULL,
    llm_call_id     BIGINT REFERENCES llm_call_log(id),
    notified_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (outlook_date, scope)
);
ALTER TABLE crypto_screening_candidates DROP COLUMN IF EXISTS last_quote_volume;

-- Telegram-команди людською мовою (orchestration/telegram_commands.py,
-- docs/decisions.md 2026-10-03) — ручний запуск джоб через Telegram
-- замість `docker compose exec app python orchestration/run_job.py`.
-- Один рядок (id=1), той самий принцип, що scheduler_heartbeat:
-- last_update_id — update_id останнього вже ОБРОБЛЕНОГО Telegram-
-- оновлення (getUpdates offset = last_update_id + 1 для наступного
-- опитування, щоб не обробити те саме повідомлення двічі).
CREATE TABLE IF NOT EXISTS telegram_command_offset (
    id             SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_update_id BIGINT NOT NULL DEFAULT 0
);

-- Watchlist — активи поза акціями S&P 500 (docs/watchlist.md, закрито
-- 2026-09-25), тепер ОДНЕ джерело істини для news/queries.py
-- (GDELT query), analysis/news_analysis/prices.py (ціна↔новини,
-- tracked_assets), orchestration/jobs.py (_watchlist_prices) і
-- reporting/watchlist_notify.py — раніше один і той самий набір був
-- хардкоджений окремо в кожному з цих файлів (docs/decisions.md
-- 2026-10-03, рішення користувача: "редагування списку обраних
-- активів через Telegram"). Seed нижче — БУКВ-У-БУКВУ той самий
-- список, що був хардкоджений — нуль зміни поведінки "з коробки".
--
-- ticker — потрібен лише для source='twelvedata' (адаптер бере тикер
-- як параметр конструктора, напр. "GBP/USD" — єдине джерело, де
-- Telegram-команда може додати НОВИЙ актив БЕЗ зміни коду:
-- fred/binance/coingecko мають фіксований словник METRICS у своєму
-- адаптері, новий актив там так і вимагає коду).
CREATE TABLE IF NOT EXISTS watchlist_assets (
    asset_id    TEXT PRIMARY KEY,
    source      TEXT NOT NULL,       -- fred | twelvedata | binance | coingecko
    metric_id   TEXT NOT NULL,
    ticker      TEXT,
    label       TEXT NOT NULL,
    search_term TEXT,                -- GDELT-термін для новин (news/queries.py); NULL = не бере участі в GDELT-запиті
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    added_via   TEXT NOT NULL DEFAULT 'seed',  -- seed | telegram
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2026-10-04 (docs/decisions.md, живий кейс "BNB" зламав ЦІЛИЙ
-- watchlist GDELT-запит): search_term тепер МОЖЕ бути NULL — коли
-- /watchlist_add live-перевірив термін через GDELT і він НЕ пройшов
-- (навіть після спроби розширити), актив усе одно додається (ціна й
-- далі збирається), просто без участі в новинному запиті. ALTER, не
-- тільки CREATE — таблиця вже існувала з NOT NULL до цієї зміни.
ALTER TABLE watchlist_assets ALTER COLUMN search_term DROP NOT NULL;

INSERT INTO watchlist_assets (asset_id, source, metric_id, ticker, label, search_term) VALUES
    ('xauusd', 'tradingeconomics', 'xauusd', 'gold', 'Золото (XAU/USD)', '"gold price"'),
    ('xagusd', 'coingecko', 'xagusd_close', NULL, 'Срібло (XAG/USD)', '"silver price"'),
    ('wti_crude', 'tradingeconomics', 'wti_crude', 'crude-oil', 'Нафта WTI', '"WTI crude"'),
    ('brent_crude', 'tradingeconomics', 'brent_crude', 'brent-crude-oil', 'Нафта Brent', '"Brent crude"'),
    ('coffee', 'tradingeconomics', 'coffee', 'coffee', 'Кава', '"coffee futures"'),
    ('eurusd', 'twelvedata', 'eurusd_close', 'EUR/USD', 'EUR/USD', '"EUR/USD"'),
    ('usdjpy', 'twelvedata', 'usdjpy_close', 'USD/JPY', 'USD/JPY', '"USD/JPY"'),
    ('btc', 'binance', 'btc_close', NULL, 'BTC/USDT', 'Bitcoin'),
    ('eth', 'binance', 'eth_close', NULL, 'ETH/USDT', 'Ethereum'),
    ('sol', 'binance', 'sol_close', NULL, 'SOL/USDT', 'Solana')
ON CONFLICT (asset_id) DO NOTHING;

-- 2026-10-04 (critical rule 7, CLAUDE.md): /watchlist_add NATGAS не
-- підтвердився ні на Twelve Data, ні на Binance (живий тест
-- користувача) — додано кодом, не через Telegram.
-- search_term — live-перевірка GDELT ще НЕ пройдена (на відміну від
-- seed-рядків вище, перевірених при оригінальному watchlist 2026-09-25) —
-- якщо GDELT відхилить, викликати common/watchlist_db.py:set_search_term(conn, 'natgas', None).
INSERT INTO watchlist_assets (asset_id, source, metric_id, ticker, label, search_term) VALUES
    ('natgas', 'tradingeconomics', 'natgas', 'natural-gas', 'Природний газ (Henry Hub)', '"natural gas price"')
ON CONFLICT (asset_id) DO NOTHING;

-- 2026-10-04 (критичне правило 7 — три послідовні живі скарги
-- користувача того самого дня, докладно docs/decisions.md): coffee/
-- wti_crude/brent_crude/natgas — ВСІ ЧОТИРИ товарні FRED-серії
-- застрягли на 2026-09-29 одночасно (FRED-публікація зупинилась, не
-- наш збір), coffee до того ж genuinely МІСЯЧНА (PCOFFOTMUSDM).
-- Проміжний крок (ручний крос-чек, source='web_crosscheck',
-- common/manual_observation.py) сам протримався лише кілька годин
-- цієї сесії — вимагав LLM-сесію на КОЖНЕ оновлення, тому фінальне
-- джерело — commodities/tradingeconomics_adapter.py (справжній
-- автоматизований скрапінг, на розкладі `_watchlist_prices`, щодня,
-- без людини). `source IN ('fred', 'web_crosscheck')` -- покриває і
-- БД, що ніколи не бачила проміжного кроку, і цю сесію, що вже на
-- ньому встигла побувати. UPDATE, не лише змінений seed вище
-- (ON CONFLICT DO NOTHING не чіпає рядок, що вже існує в БД).
UPDATE watchlist_assets
SET source = 'tradingeconomics',
    ticker = CASE asset_id
        WHEN 'coffee' THEN 'coffee'
        WHEN 'wti_crude' THEN 'crude-oil'
        WHEN 'brent_crude' THEN 'brent-crude-oil'
        WHEN 'natgas' THEN 'natural-gas'
    END
WHERE asset_id IN ('coffee', 'wti_crude', 'brent_crude', 'natgas')
  AND source IN ('fred', 'web_crosscheck');

-- 2026-10-04 (той самий механізм, ІНША причина — живий фідбек
-- користувача: синтез показав "+0.55%" для золота за тиждень, коли
-- реальний рух — зниження). Корінь — НЕ застарілість (xauusd
-- оновлювався кожен день), а НЕСТАБІЛЬНІ ревізії Twelve Data
-- forex-OTC котирування: той самий 2026-09-28 живо підтверджено
-- отримав дві ревізії з різницею 81 пункт (4196.13 -> 4115.08) ВЖЕ
-- ПІСЛЯ закриття дня -- v_observations_latest_revision бере останню,
-- не обов'язково точнішу (докладніше docs/decisions.md). ticker 'gold' —
-- URL-слаг TradingEconomics; metric_id без "_close"-суфіксу (той
-- самий принцип, що решта tradingeconomics-активів) -- стара
-- twelvedata-історія (metric_id='xauusd_close') лишається в
-- raw_observations назавжди (rule 6), просто більше не читається.
UPDATE watchlist_assets
SET source = 'tradingeconomics', metric_id = 'xauusd', ticker = 'gold'
WHERE asset_id = 'xauusd' AND source = 'twelvedata';

-- 2026-10-04: metric_forecasts — LLM-прогноз замість Python-моделі
-- (рішення користувача 2026-09-28, docs/decisions.md; PLAN.md Фаза 2 +
-- Фаза 5 "Прогнозування — LLM замість Python-моделі"). Таблиця вже
-- існувала під `method='linear_trend'` і містила ЛИШЕ число
-- (forecast_value) — для LLM-прогнозу цього мало: потрібне те саме
-- структуроване обґрунтування, що в решти LLM-виходів проєкту
-- (analysis/CLAUDE.md "Формат виходу LLM-аналізу": direction/
-- confidence/summary/reasoning + аудит-лог виклику, rule 5).
-- Усі колонки NULLABLE — старі `linear_trend`-рядки (і сам
-- `linear_trend`, що лишається naive-базовою лінією для backtest)
-- їх не мають і не повинні мати.
-- notified_at — той самий дедуп-патерн, що expectation_comparisons/
-- calendar_outlook (reporting/_common.py:mark_notified): до цієї зміни
-- metric_forecasts НІКУДИ не виходила (жодного notify-скрипта) —
-- явний пропуск, зафіксований у PLAN.md Фаза 2.
ALTER TABLE metric_forecasts ADD COLUMN IF NOT EXISTS direction TEXT;
ALTER TABLE metric_forecasts ADD COLUMN IF NOT EXISTS confidence NUMERIC;
ALTER TABLE metric_forecasts ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE metric_forecasts ADD COLUMN IF NOT EXISTS reasoning TEXT;
ALTER TABLE metric_forecasts ADD COLUMN IF NOT EXISTS llm_call_id BIGINT REFERENCES llm_call_log (id);
ALTER TABLE metric_forecasts ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;

-- 2026-10-04: trading_list — список активів, які варто РОЗГЛЯДАТИ для
-- торгівлі зараз (docs/trading-list.md, рішення користувача:
-- "це не єдиний список... активи для торгів які потрібно розглядати
-- під час сесії, тут потрібно продумати як їх відфільтровувати").
--
-- НЕ дублює screening_results/crypto_screening_candidates: ті
-- відповідають "яка компанія хороша й недорога" (215 рядків
-- фундаменталу) і "який контракт має сетап". Тут — результат
-- ФІЛЬТРА за каталізатором поверх них: актив потрапляє сюди лише
-- якщо з ним щось відбувається (реліз/новина/наш прогноз) і він
-- проходить поріг скору.
--
-- Append-only знімок, той самий принцип, що screening_results: усі
-- рядки одного прогону мають той самий run_at, "поточний список" =
-- MAX(run_at); порожній прогін нічого не вставляє — свідомо, щоб не
-- затирати попередній валідний список нульовим/збійним прогоном.
--
-- horizon — з ПЕРШОГО дня, хоча зараз заповнюється лише 'medium'
-- (рішення користувача: середньострок спочатку, інтрадей — закладене
-- розширення). Без цієї колонки інтрадей-режим вимагав би міграції
-- замість нового профілю ваг.
--
-- Внески компонентів (catalyst_score/trend_score/quality_score)
-- зберігаються ОКРЕМО, не лише фінальний score — без них неможливо
-- калібрувати ваги, бо незрозуміло, ЩО саме підняло актив у топ (той
-- самий урок, що diagnose_symbol.py для крипто-скринінгу).
CREATE TABLE IF NOT EXISTS trading_list (
    id               BIGSERIAL PRIMARY KEY,
    asset_id         TEXT NOT NULL,      -- внутрішній id: тикер акції, asset_id watchlist, символ ф'ючерса
    kind             TEXT NOT NULL,      -- stock | crypto | fx | commodity
    horizon          TEXT NOT NULL DEFAULT 'medium',  -- medium | intraday
    direction        TEXT NOT NULL,      -- up | down | neutral | unclear | conflicting
    score            NUMERIC NOT NULL,
    catalyst_score   NUMERIC,
    trend_score      NUMERIC,
    quality_score    NUMERIC,
    catalyst_summary TEXT,               -- ЧОМУ актив у списку, людською мовою
    source           TEXT NOT NULL,      -- screening | crypto_screening | watchlist
    run_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    notified_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_trading_list_run_at ON trading_list (run_at);
CREATE INDEX IF NOT EXISTS idx_trading_list_horizon ON trading_list (horizon, run_at);

-- 2026-10-04 (живий фідбек користувача на перше повідомлення торгового
-- списку: "він не читабильний а технічний, має бути нормальне пояснення
-- людською мовою а не змішано"): `catalyst_summary` був ГОТОВИМ РЯДКОМ,
-- склеєним в analysis/ — і reporting/ не мав з чого зробити людський
-- текст (metric_id замість назв, той самий реліз кілька разів, скори
-- наголо).
--
-- Тепер analysis/ віддає СТРУКТУРОВАНІ причини, а текст складає
-- reporting/ (reporting/CLAUDE.md: "тільки представлення"). Кожен
-- елемент: {kind, metric_id, label, direction, detail}.
-- `catalyst_summary` лишається — як технічний аудит-слід у БД, не для
-- повідомлення.
ALTER TABLE trading_list ADD COLUMN IF NOT EXISTS reasons JSONB;

-- Людська назва активу: "Срібло (XAG/USD)" замість "XAGUSD", назва
-- компанії замість тикера. Пишеться в analysis/ (там уже під рукою
-- `watchlist_assets.label` і `screening_results.company_name`), щоб
-- reporting/ не перезапитував три різні таблиці заради підпису.
ALTER TABLE trading_list ADD COLUMN IF NOT EXISTS label TEXT;

-- 2026-10-04: market_synthesis — ТРИ синтези на добу за сесіями
-- (спек користувача 2026-09-28: "ШІ-аналіз глобальної ситуації
-- 'враховує все' — після відкриття азіатської/європейської/
-- американської сесій, 3 рази/добу за часом сесій, не 1 раз/добу у
-- фіксовану годину").
--
-- Структурний блокер, який це знімає: `idx_market_synthesis_per_day`
-- був UNIQUE на ДАТІ, тобто фізично один рядок на добу — прогін 3×/добу
-- падав би на конфлікті (а UPSERT затирав би попередню сесію).
-- Тепер унікальність — на парі (дата, сесія).
--
-- Старі рядки (до цієї зміни) отримують session='daily' — вони й були
-- одним добовим синтезом, а не сесійним; перезаписувати історію
-- вигаданою сесією було б брехнею (rule 6: сирі дані не чіпаємо).
ALTER TABLE market_synthesis ADD COLUMN IF NOT EXISTS session TEXT NOT NULL DEFAULT 'daily';

DROP INDEX IF EXISTS idx_market_synthesis_per_day;

CREATE UNIQUE INDEX IF NOT EXISTS idx_market_synthesis_per_session
    ON market_synthesis (((created_at AT TIME ZONE 'UTC')::date), session);

-- 2026-10-04: фундаментальний LLM-аналіз акцій (спек користувача
-- 2026-09-28: "Фундаментальний ШІ-аналіз о 14:00 кожного робочого дня
-- для активів зі списку обраних — новий LLM-синтез, якого зараз немає:
-- synthesize.py робить ціна↔новини, не фундаментальний аналіз per se
-- (revenue/EPS/P/E тощо інтерпретовані LLM)").
--
-- Межа покриття, названа честно: у спеку сказано "watchlist + пройшли
-- скринінг", але у watchlist-активів (золото, нафта, газ, BTC, валютні
-- пари) фундаменталу НЕ ІСНУЄ — немає ні revenue, ні EPS, ні P/E.
-- Тому аналіз покриває лише АКЦІЇ (sec_edgar + screening_results);
-- вдавати, що товар чи валютна пара має фундаментал, було б гірше за
-- чесну межу.
--
-- `strengths`/`risks` окремими JSONB-масивами, а не всередині
-- `reasoning`: саме вони — суть фундаментального висновку, і їх треба
-- показувати списком, не шукати в абзаці тексту.
--
-- UPSERT на (ticker, день): повторний прогін того самого дня оновлює
-- рядок. `notified_at = NULL` при оновленні — той самий фікс, що
-- news_synthesis (2026-09-29): інакше оновлений висновок назавжди
-- лишався б невидимим для notify.
CREATE TABLE IF NOT EXISTS fundamental_analysis (
    id            BIGSERIAL PRIMARY KEY,
    ticker        TEXT NOT NULL,
    company_name  TEXT,
    direction     TEXT NOT NULL,     -- up | down | neutral | unclear
    confidence    NUMERIC NOT NULL,
    summary       TEXT NOT NULL,
    reasoning     TEXT NOT NULL,
    strengths     JSONB NOT NULL DEFAULT '[]'::jsonb,
    risks         JSONB NOT NULL DEFAULT '[]'::jsonb,
    inputs        JSONB NOT NULL,    -- які саме числа пішли в промпт (аудит)
    llm_call_id   BIGINT REFERENCES llm_call_log (id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    notified_at   TIMESTAMPTZ
);

-- Унікальність перенесена на (asset_id, день) —
-- `idx_fundamental_analysis_asset_per_day` нижче (2026-10-04, аналіз
-- розширено з акцій на активи watchlist). Оригінальний індекс по
-- `ticker` ВИДАЛЕНО звідси: schema.sql виконується зверху вниз, тож
-- CREATE тут відтворював би його щоразу.

-- 2026-10-04 (рішення користувача того ж дня): фундаментальний аналіз
-- розширено з АКЦІЙ на активи СПИСКУ ОБРАНИХ — "розгорнутий аналіз по
-- активах із списку вибраних і кожний актив аналізується окремо згідно
-- їхніх чинників".
--
-- Таблиця створювалась під акції (`ticker` + звітність SEC EDGAR), але
-- золото/нафта/EUR/USD/BTC тикера в цьому сенсі не мають — у них
-- `asset_id` із watchlist і ВЛАСНИЙ набір чинників
-- (config.py:ASSET_FACTORS). Тому канонічний ідентифікатор тепер
-- `asset_id`, а `ticker` лишається тільки для акцій.
ALTER TABLE fundamental_analysis ADD COLUMN IF NOT EXISTS asset_id TEXT;
ALTER TABLE fundamental_analysis ADD COLUMN IF NOT EXISTS asset_kind TEXT NOT NULL DEFAULT 'stock';
ALTER TABLE fundamental_analysis ALTER COLUMN ticker DROP NOT NULL;

-- Backfill для рядків, створених до цієї зміни (вони всі були акціями).
UPDATE fundamental_analysis SET asset_id = ticker WHERE asset_id IS NULL;

-- Унікальність перенесена з (ticker, день) на (asset_id, день).
-- Оригінальний індекс ВИДАЛЕНО звідси, а не заглушено DROP-ом нижче —
-- schema.sql виконується зверху вниз (той самий урок, що
-- idx_market_synthesis_per_day того ж дня).
DROP INDEX IF EXISTS idx_fundamental_analysis_ticker_per_day;

CREATE UNIQUE INDEX IF NOT EXISTS idx_fundamental_analysis_asset_per_day
    ON fundamental_analysis (asset_id, ((created_at AT TIME ZONE 'UTC')::date));

-- 2026-10-05: ПОШУК НОВАЧКІВ переписано (analysis/discovery/, рішення
-- користувача того ж дня: "сюди має попадати будь який актив в якого є
-- потенційне зростання або падіння, список має оновлюватися
-- автоматично"). Таблиця створювалась під ОДИН детектор —
-- LLM вигадує перспективну АКЦІЮ поза S&P 500 з новин
-- (`news_analysis/discover_candidates.py`) — і дала **0 рядків за весь
-- час**: вигадування ідеї це найважче, що можна попросити в LLM, а
-- general-потік новин забитий IPO-шумом.
--
-- Тепер кандидатів знаходять ТРИ детектори, і рух ВНИЗ — рівноправний
-- кандидат (до цього поле напрямку було просто відсутнє, бо шукались
-- лише "перспективні"):
--   `asset_class` — stock | crypto (клас активу, а не лише акції);
--   `direction`   — up | down | neutral | unclear (напрямок РУХУ, не
--                   вказівка купити/продати — analysis/CLAUDE.md);
--   `detector`    — crypto_movers | stock_movers | news_tickers (ЯКИЙ
--                   детектор знайшов; без цього неможливо сказати, чи
--                   джерело взагалі працює — саме цього й бракувало,
--                   щоб помітити "0 за весь час" раніше);
--   `score`       — зважена сила сигналу (config.py, розділ 5);
--   `signals`     — сирі числа, з яких вийшов score (рух %, спайк
--                   обсягу, к-ть джерел новин) — для калібрування
--                   порогів на живих даних.
--
-- `llm_call_id` лишається NULLABLE і для детермінованих детекторів
-- просто порожній: крипто/акційні movers — чиста арифметика без LLM
-- (правило "не викликати LLM для того, що рахується формулою",
-- analysis/CLAUDE.md).
ALTER TABLE candidate_assets ADD COLUMN IF NOT EXISTS asset_class TEXT NOT NULL DEFAULT 'stock';
ALTER TABLE candidate_assets ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'unclear';
ALTER TABLE candidate_assets ADD COLUMN IF NOT EXISTS detector TEXT NOT NULL DEFAULT 'news_tickers';
ALTER TABLE candidate_assets ADD COLUMN IF NOT EXISTS score NUMERIC;
ALTER TABLE candidate_assets ADD COLUMN IF NOT EXISTS signals JSONB;

CREATE INDEX IF NOT EXISTS idx_candidate_assets_discovered
    ON candidate_assets (discovered_at DESC);

-- 2026-10-05: ГЛОБАЛЬНИЙ (міжпроцесний) обмежувач швидкості запитів до
-- зовнішніх API — спершу для GDELT (common/rate_limit.py).
--
-- Живий збій, через який таблиця зʼявилась: потоки новин `general` і
-- `watchlist` давали НУЛЬ статей, а GDELT віддавав 429 ("Please limit
-- requests to one every 5 seconds") уже на ПЕРШИЙ запит навіть при
-- паузі 6с — IP потрапив у тривалий штрафний стан. Причина наша:
-- три джоби збору (watchlist/general/stock) стартували в межах 5
-- хвилин, КОЖНА як окремий процес, що не знає про інші, і кожна з
-- retry-бюджетом 4 внутрішні спроби × 4 ретраї runner.py — до ~100
-- запитів за кілька хвилин при ліміті 1 запит/5с. 429 у відповідь
-- запускав ЩЕ більше ретраїв — самопідсилювальний цикл.
--
-- Чому саме в БД, а не файл-лок чи пауза в коді: паузa в процесі не
-- бачить інших процесів (це й був баг), а БД у нас одна на всі
-- контейнери (app + scheduler) і вже є залежністю збору. Серіалізація
-- робиться через `pg_advisory_xact_lock` у common/rate_limit.py —
-- запит до API в один момент часу робить лише один процес.
CREATE TABLE IF NOT EXISTS api_rate_limit (
    api             TEXT PRIMARY KEY,
    last_request_at TIMESTAMPTZ NOT NULL
);

-- 2026-10-05: перехід на Anthropic для висновків (рішення користувача:
-- Sonnet 5 за замовчуванням, Opus 5 для прогнозу, DeepSeek лишається
-- на механіці — обґрунтування й заміряні обсяги в config.py розділ 9).
--
-- `provider` сам по собі перестав бути достатнім: на одному провайдері
-- тепер ДВІ моделі з різною ціною й якістю, а бектест порівнює саме
-- моделі. Без `model` неможливо сказати, чим зроблено прогноз у
-- конкретному рядку.
--
-- `fallback_from` — коли маршрут вів на Anthropic, але виклик упав
-- транспортно й пішов на резервний DeepSeek (llm_common.call_llm).
-- Потрібен, щоб "працює, але на дешевшій моделі" не виглядало як
-- норма — той самий урок, що тихий збій GDELT того ж дня.
ALTER TABLE llm_call_log ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE llm_call_log ADD COLUMN IF NOT EXISTS fallback_from TEXT;

CREATE INDEX IF NOT EXISTS idx_llm_call_log_purpose_created
    ON llm_call_log (purpose, created_at DESC);

-- 2026-10-05: ВЕРДИКТИ БЕКТЕСТУ (P0 плану покращення прогнозу).
--
-- До цього `backtest_llm.py` лише ДРУКУВАВ результат у лог — тож
-- "чи варто довіряти прогнозу на цьому показнику" знала тільки
-- людина, що читала вивід, і бойовий прогноз однаково викликав LLM
-- для всіх 19 показників. Наслідок, зміряний живо: 4 показники
-- стабільно надсилали в Telegram прогноз, ГІРШИЙ за безкоштовне
-- "нічого не змінилось", а 2 ряди не рухались жодного разу.
--
-- Append-only журнал ПРОГОНІВ (не "поточний стан"): три прогони того
-- самого показника різними моделями — три рядки, і саме їхнє
-- порівняння й показало, що розкид між моделями більший за перевагу
-- будь-якої з них. Перезатирати це означало б втратити єдиний
-- доказ, на якому тримається вибір методу.
--
-- `model` обов'язковий поруч із `provider`: на одному провайдері дві
-- моделі з різною ціною й різним результатом (Opus 5 vs Sonnet 5).
CREATE TABLE IF NOT EXISTS backtest_verdicts (
    id             BIGSERIAL PRIMARY KEY,
    metric_id      TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    effort         TEXT,                -- лише Anthropic; NULL для DeepSeek
    n_points       INTEGER NOT NULL,
    n_skipped      INTEGER NOT NULL,
    llm_mae        NUMERIC,
    naive_mae      NUMERIC,
    trend_mae      NUMERIC,
    -- llm_wins | naive_wins | tie | flat_series | noise
    -- (межа "перемога vs шум" — config.py:FORECAST_MEANINGFUL_EDGE_PCT)
    verdict        TEXT NOT NULL,
    advantage_pct  NUMERIC,             -- +27.5 = LLM на 27.5% точніший за naive
    run_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_backtest_verdicts_metric
    ON backtest_verdicts (metric_id, model, run_at DESC);

-- 2026-10-06: СТАТУС новинного терміну watchlist-активу.
--
-- Живий фідбек користувача: "я тут в запиті не бачу активів, які були
-- добавлені в список обраних через телеграм". Справді: `bnb` мав
-- `search_term = NULL`, тож не брав участі в GDELT-запиті — і ніщо
-- ніколи не пробувало знову.
--
-- Корінь не в самому `bnb`, а в тому, що ДВА РІЗНІ результати
-- зберігались однаково (NULL):
--   1. GDELT відмовив у терміні ("занадто коротка фраза" — той самий
--      "Uber"-урок, 2026-09-25) — це остаточна відмова;
--   2. перевірку НЕ ВДАЛОСЬ провести (429/мʼякий троттл/мережа) — це
--      "невідомо", і воно мусить бути тимчасовим.
-- З баном GDELT (2026-10-05) другий випадок став постійним: кожен
-- новий актив, доданий через Telegram, назавжди лишався б без новин,
-- мовчки. Рівно той клас тихого збою, що й мʼякий троттл сам.
--
-- Тепер термін зберігається ЗАВЖДИ, а участь у запиті визначає статус:
--   'validated' — GDELT прийняв, іде в спільний запит;
--   'pending'   — перевірку не вдалось провести, НЕ йде в запит,
--                 але джоба `revalidate_news_terms` пробує знову;
--   'rejected'  — GDELT відмовив остаточно, НЕ йде в запит.
-- Один поганий OR-терм і далі не може зіпсувати запит для всіх
-- активів (причина оригінального рішення 2026-10-04) — у запит
-- потрапляє лише 'validated'.
ALTER TABLE watchlist_assets
    ADD COLUMN IF NOT EXISTS search_term_status TEXT NOT NULL DEFAULT 'validated';

-- Наявні рядки: термін є -> він свого часу пройшов перевірку.
UPDATE watchlist_assets SET search_term_status = 'validated'
WHERE search_term IS NOT NULL AND search_term_status <> 'validated';

-- `bnb` — єдиний актив із втраченим терміном на момент міграції.
-- Ставимо КАНДИДАТА з контекстним словом ("BNB" сам по собі GDELT
-- відхиляв як занадто коротку фразу, telegram_commands.py:
-- _try_add_via_binance додає "crypto") і статус 'pending', щоб
-- re-validation спробувала його, коли GDELT стане доступним. Якщо
-- відмовить — статус сам стане 'rejected', і це буде видно.
UPDATE watchlist_assets
SET search_term = '"BNB crypto"', search_term_status = 'pending'
WHERE asset_id = 'bnb' AND search_term IS NULL;

-- 2026-10-06: ЯКИМ ПРОМПТОМ зміряно вердикт.
--
-- Двічі за один день те саме: зміна промпту/вікна посеред серії
-- прогонів робила таблицю результатів несумісною самою з собою, і
-- побачити це можна було лише за годинниками в лозі (спершу вікно
-- історії 24 проти всієї, потім поява таблиці Δ). `provider`/`model`
-- цього не описують — промпт змінюється незалежно від моделі.
--
-- Значення — константа `llm_forecast.PROMPT_VARIANT`: її МУСИТЬ
-- змінити той, хто змінює промпт, інакше два різні виміри
-- виглядатимуть як один.
ALTER TABLE backtest_verdicts ADD COLUMN IF NOT EXISTS prompt_variant TEXT;


-- Відпечаток останнього НАДІСЛАНОГО складу для кожного notify-скрипта
-- (2026-10-13). Призначення: часові розсилки мусять шле лише ЗМІНУ.
--
-- Живий привід. Вивід за 12 жовтня: `notify_screening` надіслав
-- ІДЕНТИЧНИЙ список із 14 тикерів 13-й день підряд (той самий склад,
-- той самий порядок); `notify_candidates` — ті самі 6 акцій (CCI,
-- SBAC, MRNA, AMT, HUM, VST) ТРИЧІ за добу з однаковими числами, бо
-- ціни акцій оновлюються раз на добу о 03:00; `notify_crypto_long` і
-- `notify_crypto_screening` — той самий 1-2 кандидати 8 разів за
-- добу. Разом ~25 повідомлень, з яких зі справді новим змістом — 6.
--
-- Чому цього не вирішував наявний `notified_at` (reporting/_common.py:
-- mark_notified): він дедупить РЯДКИ, а скринінг/новачки щопрогону
-- створюють НОВІ рядки з новим `run_at` — технічно не надіслані,
-- змістово ті самі. Відпечаток працює на рівні СКЛАДУ, не рядків.
--
-- `fingerprint` — хеш відсортованого списку ідентифікаторів (тикери/
-- символи/asset_id). Відсортованого свідомо: зміна лише ПОРЯДКУ при
-- тому самому складі не є новиною для читача.
CREATE TABLE IF NOT EXISTS notify_fingerprints (
    notifier    TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    item_count  INTEGER NOT NULL DEFAULT 0,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- Текст звітів 10-K/10-Q + витяг тез (2026-10-13).
--
-- ОСТАННЯ невиконана обіцянка опису агента ("збирає ... звіти
-- компаній"): `companies/sec_edgar_adapter.py` збирав лише ЧИСЛОВІ
-- факти XBRL, а текст звіту — ніколи. Числа кажуть ЩО сталося, MD&A
-- каже ЧОМУ, і це єдине місце, де компанія сама називає причини.
--
-- Живий привід: фундаментальний аналіз 12 жовтня переказував
-- квартальні цифри ("виручка зросла з 3.69 до 5.04 млрд") і не міг
-- пояснити причину — у звіті вона написана словами.
--
-- `full_text` зберігається цілком (~92 КБ на звіт у живому замірі):
-- критичне правило 6 — сирі дані append-only, і якщо завтра
-- знадобиться інший розділ, перезавантажувати з SEC не доведеться.
-- `sections` — витягнуті розділи (mdna/risk_factors) у jsonb.
CREATE TABLE IF NOT EXISTS company_filings (
    id           BIGSERIAL PRIMARY KEY,
    ticker       TEXT NOT NULL,
    cik          BIGINT NOT NULL,
    form         TEXT NOT NULL,
    filing_date  DATE NOT NULL,
    accession    TEXT NOT NULL,
    url          TEXT NOT NULL,
    full_text    TEXT NOT NULL,
    sections     JSONB NOT NULL DEFAULT '{}'::jsonb,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (accession, ticker)
);

CREATE INDEX IF NOT EXISTS idx_company_filings_ticker_date
    ON company_filings (ticker, filing_date DESC);

-- Тези, витягнуті LLM із тексту звіту. Окрема таблиця, не стовпці в
-- `company_filings`: сирий текст і висновок про нього — різні сорти
-- даних із різним життєвим циклом (текст незмінний назавжди, витяг
-- можна перегенерувати іншою моделлю чи промптом), і правило 5
-- вимагає логувати КОЖЕН LLM-виклик окремо (`llm_call_id`).
CREATE TABLE IF NOT EXISTS filing_insights (
    id            BIGSERIAL PRIMARY KEY,
    filing_id     BIGINT NOT NULL REFERENCES company_filings(id) ON DELETE CASCADE,
    ticker        TEXT NOT NULL,
    summary       TEXT NOT NULL,
    drivers       JSONB NOT NULL DEFAULT '[]'::jsonb,
    risks         JSONB NOT NULL DEFAULT '[]'::jsonb,
    changes       JSONB NOT NULL DEFAULT '[]'::jsonb,
    direction     TEXT,
    confidence    NUMERIC,
    llm_call_id   BIGINT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    notified_at   TIMESTAMPTZ,
    UNIQUE (filing_id)
);


-- Заплановані виступи членів центробанків (2026-10-13).
--
-- ЧОМУ З'ЯВИЛАСЬ. Постановка користувача 2026-10-11: "ставка
-- прогнозується після виходу нових макроекономічних даних + новини +
-- виступи банків та голів комісій". Виступи вже ПРИХОДИЛИ у фіді
-- подій (живий замір 13 жовтня: 31 запис із 110 — "FOMC Member Waller
-- Speaks" із датою та impact), але нікуди не писались — пролітали
-- мимо на кожному прогоні `refresh_calendar`.
--
-- Окрема таблиця, не `release_log`: виступ не є РЕЛІЗОМ ДАНИХ. У
-- нього немає ні значення, ні очікування, ні факту для порівняння —
-- тобто весь контракт release_log (pending→detected→processed,
-- expected_value) до нього не застосовний. Спільне в них лише те, що
-- обидва мають дату з того самого фіду.
CREATE TABLE IF NOT EXISTS policy_speeches (
    id            BIGSERIAL PRIMARY KEY,
    source        TEXT NOT NULL,
    title         TEXT NOT NULL,
    speaker       TEXT,
    institution   TEXT,
    scheduled_at  TIMESTAMPTZ NOT NULL,
    impact_level  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (title, scheduled_at)
);

CREATE INDEX IF NOT EXISTS idx_policy_speeches_scheduled
    ON policy_speeches (scheduled_at DESC);


-- Повний синтез ставки: прогноз РІШЕННЯ засідання (2026-10-13).
--
-- Останній незакритий пункт плану (постановка користувача 2026-10-11:
-- "ставка прогнозується після виходу нових макроекономічних даних +
-- новини + виступи банків та голів комісій").
--
-- Чому ОКРЕМА таблиця, а не `metric_forecasts`: там одиниця — число
-- на наступний період, тут — РІШЕННЯ (hold/cut/hike) на конкретну
-- дату засідання. Це зміряно важлива різниця: точковий прогноз
-- ставки "на завтра" давав нуль помилки і в naive, і в ШІ (ставка
-- між засіданнями не змінюється), тобто формально ідеальний
-- результат без жодної передбачувальної цінності.
--
-- `UNIQUE (bank, meeting_at)` — один синтез на засідання; повторний
-- прогін оновлює його новішими входами (нові дані/виступи до того ж
-- засідання) і скидає `notified_at`, щоб оновлений висновок дійшов.
CREATE TABLE IF NOT EXISTS policy_rate_synthesis (
    id                BIGSERIAL PRIMARY KEY,
    bank              TEXT NOT NULL,
    metric_id         TEXT NOT NULL,
    meeting_at        TIMESTAMPTZ NOT NULL,
    decision          TEXT NOT NULL,
    expected_rate     NUMERIC,
    confidence        NUMERIC,
    summary           TEXT NOT NULL,
    reasoning         TEXT,
    watch_on_release  TEXT,
    llm_call_id       BIGINT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    notified_at       TIMESTAMPTZ,
    UNIQUE (bank, meeting_at)
);

-- ============================================================
-- ЖУРНАЛ НАСЛІДКІВ (2026-10-15, рішення користувача)
-- ============================================================
-- Агент видавав списки, вердикти й напрямки — і НІКОЛИ не перевіряв,
-- що з них вийшло. Постфактум-оцінка існувала лише для прогнозів
-- показників (`backtest_verdicts`, ШІ проти naive); LONG/SHORT, список
-- торгів, новачки й напрямки синтезів не мали її взагалі. Тобто ваги й
-- пороги в `config.py` лишались припущеннями, які нічим не перевірити.
--
-- Таблиця НЕ має FK на таблиці рішень: `decision_id` — слабке
-- посилання, бо знімкові таблиці підлягають прибиранню
-- (`prune_derived`), а наслідок має його переживати. Тому ціна й
-- вердикт копіюються сюди значеннями.
CREATE TABLE IF NOT EXISTS decision_outcomes (
    id                 BIGSERIAL PRIMARY KEY,
    decision_kind      TEXT NOT NULL,     -- trading_list | crypto_long | crypto_watch | candidate
    decision_id        BIGINT NOT NULL,   -- id рядка в таблиці-джерелі (слабке посилання)
    asset_key          TEXT NOT NULL,     -- asset_id/ticker/symbol як його знає джерело
    asset_class        TEXT NOT NULL,     -- stock | crypto | commodity | fx
    direction          TEXT NOT NULL,     -- up | down | long | short | watch | unclear
    decided_at         TIMESTAMPTZ NOT NULL,
    horizon_days       INTEGER NOT NULL,
    price_at_decision  NUMERIC,
    price_at_eval      NUMERIC,
    realized_pct       NUMERIC,
    verdict            TEXT NOT NULL,     -- correct | incorrect | flat | unavailable
    detail             TEXT,
    evaluated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    notified_at        TIMESTAMPTZ,
    UNIQUE (decision_kind, decision_id, horizon_days)
);

CREATE INDEX IF NOT EXISTS idx_decision_outcomes_kind_eval
    ON decision_outcomes (decision_kind, evaluated_at DESC);

-- ============================================================
-- ЗДОРОВ'Я ВИВОДІВ (2026-10-15, рішення користувача)
-- ============================================================
-- Домінантний клас дефектів проєкту — "джоба працює, вивід
-- вироджений": шість випадків 2026-10-13 і два 2026-10-15, усі
-- знайдені ВИМІРОМ руками, жоден не впав сам. `freshness.py` відповідає
-- на питання про ЧАС, `quality.py` — про ПЕРЕЗАПИС, а на питання "а чи
-- вивід узагалі різнить щось" не відповідав ніхто.
--
-- Один рядок = один прогін однієї перевірки. Історія потрібна, щоб
-- алерт ішов лише при ЗМІНІ стану: перевірка, що щодня звітує "усе
-- гаразд", стає шумом (той самий урок, що звірка FRED↔BLS і прапорець
-- нестабільних ревізій).
CREATE TABLE IF NOT EXISTS output_health_checks (
    id            BIGSERIAL PRIMARY KEY,
    check_name    TEXT NOT NULL,
    target_table  TEXT NOT NULL,
    status        TEXT NOT NULL,          -- ok | fail | skipped
    measured      JSONB NOT NULL,         -- що саме зміряно (рядки, distinct, частка NULL)
    detail        TEXT NOT NULL,
    checked_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_output_health_name_time
    ON output_health_checks (check_name, checked_at DESC);

-- ============================================================
-- КАЛЕНДАР ЗВІТНОСТІ КОМПАНІЙ (2026-10-15, рішення користувача)
-- ============================================================
-- Дат звітності не було взагалі: EPS використовувався як ЦИФРА в
-- Tier B, але "коли вийде наступний звіт" агент не знав. Релізний
-- календар покриває лише 15 макропоказників, тож у `catalyst_score`
-- акції мали прицільну подію тільки за наявності новини — зміряно
-- 2026-10-13: власну подію мали 9 кандидатів із 29, решта стояла на
-- спільному макрофоні.
--
-- Джерело — Nasdaq earnings calendar (неофіційний публічний endpoint,
-- той самий клас, що ForexFactory для макро: єдине безкоштовне, що
-- дає МАЙБУТНЮ дату; Twelve Data віддає лише минулі звіти без
-- платного плану — перевірено живим запитом 2026-10-15, 403).
CREATE TABLE IF NOT EXISTS earnings_calendar (
    id            BIGSERIAL PRIMARY KEY,
    ticker        TEXT NOT NULL,
    report_date   DATE NOT NULL,
    report_time   TEXT,                   -- before/after market, як віддає джерело
    eps_forecast  NUMERIC,
    company_name  TEXT,
    source        TEXT NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (ticker, report_date)
);

CREATE INDEX IF NOT EXISTS idx_earnings_calendar_date
    ON earnings_calendar (report_date);

-- ============================================================
-- КОМПРЕСІЯ raw_observations — УВІМКНЕНА 2026-10-15, ВІДКОЧЕНА 2026-10-16
-- ============================================================
-- ЖИВИЙ ЗБІЙ У ПРОДАКШЕНІ. Компресія (`segmentby = source`,
-- `compress_after = 30 days`) відпрацювала один раз і стиснула 1205 із
-- 1211 чанків. Через ~9 годин ядро почало вбивати бекенди Postgres:
--   server process ... was terminated by signal 9: Killed
--   the database system is in recovery mode
-- Провалились `screening_composite_score` і `news_collect_stock`
-- (обидві через `run_tier_a` → `_batch_db.batch_latest_values`).
--
-- МЕХАНІЗМ. `v_current_values` — це `DISTINCT ON` БЕЗ обмеження по
-- `observed_at`, тож запит обов'язково торкається ВСІХ чанків (це
-- задокументовано в `analysis/screening/_batch_db.py` ще з 2026-09-25).
-- Після компресії кожен стиснутий чанк додає у план власні вузли
-- розпакування. Зміряно на деві: запит на **40** metric_id дає план із
-- **8515 вузлів** і 4.2 с планування при 0.4 с виконання. У
-- продакшені той самий запит іде по **512** тикерах — план
-- розростається приблизно в 12 разів, і бекенд гине на ПЛАНУВАННІ, не
-- на даних.
--
-- ЧОГО Я НЕ ЗМІРЯВ, УВІМКНУВШИ ЦЕ. Було зміряно: коефіцієнт стиснення
-- (3280 кБ → 464 кБ, 7.1×) і безпека ЗАПИСУ (вставка/ревізія/дедуп/
-- UNIQUE у стиснутий чанк). Не зміряно головне — ЧИТАННЯ бойовими
-- запитами агента. Тобто перевірено було те, що зручно перевірити, а
-- не те, що ламається (пряме порушення власного правила проєкту:
-- міряти продакшен-форму запиту ДО і ПІСЛЯ).
--
-- ЩО МУСИТЬ БУТИ ПРАВДОЮ, ЩОБ УВІМКНУТИ ЗНОВУ (не вмикати без цього):
--   1. гарячі запити мають МЕЖУ ДАТИ, щоб планувальник відсікав чанки
--      (як зроблено в `crypto_screening/_history_db.py`: літеральна
--      дата → планування 1877 мс → 13.5 мс);
--   2. або `chunk_time_interval` збільшено так, щоб чанків були
--      десятки, а не 1211;
--   3. `segmentby` включає `metric_id` (інакше фільтр по metric_id не
--      дає відсікання сегментів і розпаковує все джерело);
--   4. зміряно `EXPLAIN (ANALYZE)` на 512 metric_id ПІСЛЯ компресії —
--      і планування лишилось у межах секунд.
--
-- Корективна міграція: знімає політику на вже розгорнутих базах, щоб
-- `apply_schema.py` сам припинив рекомпресію. Розпакування наявних
-- чанків — окремою дією (`scripts/decompress_raw_observations.sh`):
-- це довга операція над даними, не схема.
SELECT remove_compression_policy('raw_observations', if_exists => TRUE);

-- ============================================================
-- ТРИ ЧИСЛА ПОРУЧ (2026-10-15, рішення користувача)
-- ============================================================
-- Прогноз агента, факт і ринкове очікування жили в РІЗНИХ
-- повідомленнях: `forecast_notify.py` шле прогноз, `expectations_notify.py`
-- — факт проти очікування. Побачити «агент побив naive, але програв
-- календарному» можна було лише SQL-запитом.
--
-- ЧОМУ ЦЕ НЕ ПРОСТО ФОРМАТУВАННЯ. Прогноз лежить у `metric_forecasts`
-- РІВНЕМ (cpi 334.9), а порівняння з очікуванням — у m/m % (0.6).
-- Щоб три числа були зрівнянні, прогноз треба прогнати ТИМ САМИМ
-- методом, що й факт (`comparison_methods.compute_actual`), тож
-- обчислення належить шару аналізу, а не reporting/.
--
-- ЖИВИЙ УРОК, через який це взагалі робиться кодом: вручну я взяв
-- прогноз cpi 337.1 (створений 14.10) як прогноз вересневого релізу —
-- а в нього `based_on_observed_at = 2026-09-01`, тобто він зроблений
-- ПІСЛЯ релізу й прогнозує ЖОВТЕНЬ. Правильний рядок — 334.9 від
-- 06.10 на серпневих даних. Вибір прогнозу неочевидний, і саме тому
-- він тепер у коді з умовою `based_on_observed_at < observed_at`.
ALTER TABLE expectation_comparisons
    ADD COLUMN IF NOT EXISTS forecast_value_parsed NUMERIC;
ALTER TABLE expectation_comparisons
    ADD COLUMN IF NOT EXISTS forecast_method TEXT;
-- closer_than_calendar | farther_than_calendar | tie — хто був ближчий до
-- факту. Вердикт зберігається, а не рахується в reporting/: там лише
-- форматування (reporting/CLAUDE.md).
ALTER TABLE expectation_comparisons
    ADD COLUMN IF NOT EXISTS forecast_verdict TEXT;

-- ============================================================
-- ФАКТОРНІ РЯДИ АКТИВІВ І ЧЕК-ЛИСТИ ДЛЯ БУДЬ-ЯКОГО АКТИВУ (2026-10-20)
-- ============================================================
-- Рішення користувача: для кожного активу, крім загальних
-- каталізаторів, потрібні його СПЕЦИФІЧНІ дані (запаси, потоки в ETF,
-- он-чейн, погода) — зі звіркою на кількох джерелах і контролем
-- свіжості; і кожен актив, включно з доданим через Telegram, має мати
-- власний чек-лист чинників (config.py, розділи 27-28).

INSERT INTO sources (name, category, source_type, notes) VALUES
    ('eia', 'commodities', 'official_primary', 'U.S. Energy Information Administration API v2 — щотижневі запаси нафти без SPR (WCESTUS1) і газу в сховищах (NW2_EPG0_SWO_R48_BCF). EIA_API_KEY, без нього DEMO_KEY (~10 запитів/год на IP). Звірка — тижнева зміна з tradingeconomics'),
    ('spdr_gld', 'commodities', 'official_primary', 'SPDR Gold Shares — архів емітента (xlsx), тонн золота у фонді GLD щодня. Одне джерело: це власна книга емітента'),
    ('coinmetrics', 'crypto', 'aggregator', 'CoinMetrics Community API — транзакцій за добу BTC/ETH, без ключа. Для SOL мережевих метрик безкоштовно немає'),
    ('blockchain_info', 'crypto', 'aggregator', 'blockchain.info Charts API — транзакцій BTC за добу, без ключа, лаг ~6 днів. Друге джерело для звірки з coinmetrics (розкид 0.3-1.8%)'),
    ('etherscan', 'crypto', 'aggregator', 'Etherscan — CSV графіка щоденних транзакцій ETH, без ключа. Друге джерело для звірки з coinmetrics (живо — точний збіг)'),
    ('farside', 'crypto', 'unofficial', 'Farside Investors — скрапінг HTML-таблиць щоденних чистих потоків у спотові BTC/ETH-ETF США ($ млн). Одне джерело: зведення по всіх фондах безкоштовно публікує лише Farside'),
    ('defillama', 'crypto', 'aggregator', 'DefiLlama API — щоденний TVL мережі Solana, без ключа. Одне джерело: інші агрегатори TVL платні або з іншою методологією'),
    ('open_meteo', 'weather', 'aggregator', 'Open-Meteo — прогноз погоди на 7 днів для погодних індексів товарів (HDD США для газу; мін. температура й опади кавових регіонів Бразилії/Вʼєтнаму), без ключа'),
    ('met_norway', 'weather', 'official_primary', 'MET Norway Locationforecast 2.0 — друге незалежне джерело прогнозу погоди для звірки з open_meteo. Обовʼязковий User-Agent із контактом'),
    ('ice', 'commodities', 'official_primary', 'ICE Futures U.S. — щоденний звіт про сертифіковані запаси кави C (xls BIFF, xlrd). Облік веде лише біржа — одне джерело'),
    ('usda_psd', 'commodities', 'official_primary', 'USDA FAS PSD — відкритий архів psd_coffee_csv.zip без ключа: оцінки врожаю кави Бразилії й Вʼєтнаму за маркетинговий рік, переглядаються двічі на рік'),
    ('ishares_iau', 'commodities', 'official_primary', 'iShares Gold Trust (IAU) — тонни золота у фонді зі сторінки емітента; перевіряється внутрішня узгодженість тонни ↔ унції'),
    ('mof_japan', 'macro', 'official_primary', 'Мінфін Японії — CSV валютних інтервенцій із 1991 року (CP932, 100 млн ієн); денні дані публікуються щокварталу'),
    ('beacon_lighthouse', 'crypto', 'official_primary', 'Консенсусний шар Ethereum через вузол Lighthouse (publicnode.com) — баланс активних валідаторів, тобто обсяг ETH у стейкінгу. Дані самого блокчейну, один вузол'),
    ('solana_publicnode', 'crypto', 'official_primary', 'Solana RPC (publicnode.com) — темп не-голосувальних транзакцій за останні 240 зразків (~4 год)'),
    ('solana_mainnet', 'crypto', 'official_primary', 'Solana RPC (офіційний api.mainnet-beta.solana.com) — друге джерело для звірки з solana_publicnode, те саме вікно')
ON CONFLICT (name) DO NOTHING;

-- Вердикт звірки факторного ряду між джерелами (common/consensus.py)
-- на дату останньої спільної точки. Похідне: перераховується щодня з
-- raw_observations (monitoring/crosscheck_factors.py), прибирається
-- prune_derived. Читається аналізом, щоб поруч із числами в промпті
-- стояло, наскільки їм можна вірити.
CREATE TABLE IF NOT EXISTS factor_verdicts (
    metric_id      TEXT NOT NULL,
    observed_at    DATE NOT NULL,
    -- agree | outlier | disagree | insufficient (одне джерело)
    status         TEXT NOT NULL,
    consensus      NUMERIC,
    max_spread_pct NUMERIC,
    -- {джерело: значення}; для compare='diff' — уже приведені до зміни
    source_values  JSONB,
    detail         TEXT,
    checked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- коли про розбіжність по цій точці вже надіслано тривогу
    notified_at    TIMESTAMPTZ,
    PRIMARY KEY (metric_id, observed_at)
);

-- Чек-лист чинників, складений ШІ для активу без ручного чек-листа в
-- config.ASSET_FACTORS. Один рядок на актив; складається наново, коли
-- змінився каталог доступних рядів (catalog_fingerprint), а не щодня.
-- Не прибирається за розкладом: живе, доки актив у watchlist.
CREATE TABLE IF NOT EXISTS asset_checklists (
    asset_id            TEXT PRIMARY KEY,
    -- [[ключ, підпис], ...] у порядку промпта, вже перевірені каталогом
    factors             JSONB NOT NULL,
    -- драйвери, яких у каталозі немає (чесні прогалини)
    gaps                JSONB NOT NULL,
    -- ключі, які ШІ запропонував, але їх немає в каталозі (аудит)
    rejected            JSONB,
    catalog_fingerprint TEXT NOT NULL,
    llm_call_id         BIGINT REFERENCES llm_call_log (id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
