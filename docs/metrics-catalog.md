# Каталог показників

Робочий список того, що плануємо збирати для повного аналізу. Це
карта на кілька фаз наперед, не чекліст "усе одразу" — беремо
показники частинами під час Фази 1, звужуючи чи розширюючи список
за потреби.

**Принцип (не міняється фазами):** тут зберігаються тільки сирі
факти — числа як є від джерела, без інтерпретації. Розрахунки типу
MoM/YoY % зміни, порівняння факт/консенсус, тренди — це відповідальність
`analysis/`, не `data-ingestion/`. Див. `docs/decisions.md` запис
"revision обчислює шар збереження" — та сама логіка розділення
застосовується і тут: `data-ingestion/` збирає, `analysis/` рахує.

## Макро — США (джерело: FRED, адаптер уже є — `macro/fred_adapter.py`)

| Показник | FRED series_id | Частота | Статус |
|---|---|---|---|
| CPI (інфляція) | `CPIAUCSL` | місячна | ✅ зібрано (Фаза 0) |
| Core CPI (без їжі/енергії) | `CPILFESL` | місячна | ✅ адаптер готовий (metric_id `core_cpi`) |
| PCE Price Index (орієнтир ФРС) | `PCEPI` | місячна | ✅ адаптер готовий (metric_id `pce_price_index`) |
| Fed Funds Rate | `DFF` | щоденна | ✅ адаптер готовий (metric_id `fed_funds_rate`) |
| 10Y Treasury Yield | `DGS10` | щоденна | ✅ адаптер готовий (metric_id `treasury_10y`) |
| 2Y Treasury Yield | `DGS2` | щоденна | ✅ адаптер готовий (metric_id `treasury_2y`, разом з 10Y — спред 10Y-2Y, індикатор рецесії) |
| Unemployment Rate | `UNRATE` | місячна | ✅ адаптер готовий (metric_id `unemployment_rate`) |
| Non-Farm Payrolls | `PAYEMS` | місячна | ✅ адаптер готовий (metric_id `nonfarm_payrolls`) |
| Initial Jobless Claims | `ICSA` | щотижнева | ✅ адаптер готовий (metric_id `initial_jobless_claims`) |
| Real GDP | `GDPC1` | квартальна | ✅ адаптер готовий (metric_id `real_gdp`) |
| Retail Sales | `RSAFS` | місячна | ✅ адаптер готовий (metric_id `retail_sales`) |
| Housing Starts | `HOUST` | місячна | ✅ адаптер готовий (metric_id `housing_starts`) |
| Mortgage Rates (30Y Fixed) | `MORTGAGE30US` | щотижнева | ✅ адаптер готовий (metric_id `mortgage_rate_30y`) |
| USD/JPY (для carry trade разом з fed_funds_rate/japan_policy_rate) | `DEXJPUS` | щоденна | ✅ адаптер готовий (metric_id `usdjpy_fx_rate`; Fed H.10 release, не OECD MEI — та серія й далі оновлюється, на відміну від застарілих OECD-серій Японії, див. нижче) |

## Макро — єврозона (джерело: ECB Data Portal, адаптер уже є — `macro/ecb_adapter.py`)

| Показник | ECB flowRef.seriesKey | Частота | Статус |
|---|---|---|---|
| HICP (аналог CPI) | `HICP.M.U2.N.000000.4D0.ANR` | місячна | ✅ адаптер готовий (metric_id `eurozone_hicp`; датасет `ICP` закрито ECB 4 лют. 2026, замінено на `HICP`) |
| Deposit Facility Rate (аналог Fed Funds Rate) | `FM.D.U2.EUR.4F.KR.DFR.LEV` | щоденна | ✅ адаптер готовий (metric_id `eurozone_deposit_rate`) |
| Unemployment Rate | `LFSI.M.U2.S.UNEHRT.TOTAL0.15_74.T` | місячна | ✅ адаптер готовий (metric_id `eurozone_unemployment_rate`) |

## Макро — Японія (Китай/Індія свідомо не розглядаємо — рішення користувача 2026-08-30, немає офіційного API, див. `docs/decisions.md`)

| Показник | Джерело | Деталі доступу | Статус |
|---|---|---|---|
| Policy Rate (Uncollateralized O/N Call Rate) | Bank of Japan Time-Series Data Search API | Без ключа. `db=FM01`, код серії `STRDCLUCON`. `macro/boj_adapter.py`, metric_id `japan_policy_rate`. | ✅ адаптер готовий, **живий прогін підтверджено користувачем 2026-08-30** (5 записів у raw_observations) |
| CPI (інфляція) | e-Stat (政府統計の総合窓口) API v3.0, `getStatsData` | Потрібен `appId` (`ESTAT_APP_ID` в .env) — **користувач уже додав ключ**. Статистична таблиця: 2025年基準消費者物価指数 (база 2025=100, чинна з 21.08.2026), `statsDataId=0004052037`. `macro/estat_adapter.py`, metric_id `japan_cpi`. Коди area="全国"/cat01="総合" не хардкодяться — адаптер резолвить їх сам через метадані CLASS_INF при кожному запиті (двоетапний fetch: спершу метадані, тоді дані). | ✅ живий прогін пройшов (2026-09-13, після перезбирання Docker — початковий DNS-збій виявився транзієнтним); під час прогону знайдено і виправлено реальний баг зі змішуванням індексу й %-змін через нерезолвлений вимір `tab` (деталі, фікс і нові тести — `docs/decisions.md`) |
| USD/JPY (для carry trade) | FRED, `DEXJPUS` (Fed H.10, не OECD MEI) | Через уже наявний `macro/fred_adapter.py`. metric_id `usdjpy_fx_rate`. | ✅ адаптер готовий (свідомо через FRED, а не BOJ FM08 — щоб не тримати дві неперевірені структури відповіді одночасно) |

Carry trade Японія/США: `fed_funds_rate` (FRED) − `japan_policy_rate`
(BOJ) = диференціал ставок; `usdjpy_fx_rate` (FRED) — сам курс. Сам
диференціал — розрахунок, належить `analysis/` (Фаза 2), тут лише
збір трьох сирих компонентів.

**Наступний крок:** живий тест `japan_cpi` через Docker — якщо
структура відповіді e-Stat відрізняється від очікуваної
(`_resolve_area_and_cat01` кине ValueError з описом, що саме не
знайдено), надішліть повідомлення про помилку чи сирий JSON, і
`normalize()`/`_resolve_area_and_cat01()` буде виправлено за тим же
протоколом, що й для ECB/BOJ.

## Випереджаючі індикатори (джерело — TBD, не в FRED напряму)

- ISM Manufacturing PMI
- ISM Services PMI

## Quotes / котирування акцій (джерело: Twelve Data — `quotes/twelvedata_adapter.py`, ✅ адаптер готовий)

На відміну від macro/* — тут немає фіксованого списку показників.
`FIELDS` в адаптері фіксує лише **поля одного бару** (`close`,
`volume`), а тикер — параметр виклику (`--source twelvedata --ticker
AAPL` у `run_collect.py`, а не `--metric`). `metric_id` збирається
динамічно як `{ticker}_{field}`, напр. `aapl_close`, `aapl_volume`.

Третій кандидат на джерело (Alpaca — гео-блок, Stooq — JS-based
бот-захист з вересня 2026, обидва відкинуто без коду в продакшн).
Twelve Data — перше, підтверджене живим запитом користувача (200,
реальні дані AAPL) ДО написання адаптера. Ліміти free tier: ~8
запитів/хв, 800/добу — прогін скринера на весь S&P 500 (~500
тикерів × 1 credit) укладається в добову квоту (~63 хв на повний
прогін через ліміт запитів/хв).

Використовується як джерело ціни/обсягу для скринінгу акцій — деталі
критеріїв, порогів і universe (S&P 500) в `config.py`.

## Компанії (джерело: SEC EDGAR — `companies/sec_edgar_adapter.py`, ✅ адаптер готовий)

Як і Stooq/Twelve Data — немає фіксованого METRICS-словника
показників, тільки словник **концептів** (`revenue`, `net_income`,
`eps_diluted`, `shares_outstanding`, `assets`, `liabilities` → us-gaap/
dei tag). Тикер — параметр виклику (`--source sec_edgar --ticker
AAPL`), не хардкод. `metric_id` = `{ticker}_{concept}`, напр.
`aapl_revenue`. CIK резолвиться через `company_tickers.json` (кеш на
процес) або передається явно.

Офіційне джерело (не виняток з "official sources first" — на відміну
від quotes/*): без ключа, але обов'язковий контактний User-Agent
(`SEC_EDGAR_USER_AGENT`, інакше 403).

Формат відповіді підтверджено кількома незалежними технічними
джерелами, але НЕ живим запитом (sec.gov немає в дозволених доменах
пісочниці Claude) — перший прогін користувача ще не підтверджено.

Відомий ризик (знайдено через аналогію з минулим багом japan_cpi):
XBRL duration-концепти мають і квартальні, і кумулятивні записи з
однаковим "end"-днем — `normalize()` фільтрує за довжиною періоду
(~80-100 днів), щоб не змішати їх під одним metric_id/датою.

Відомий пробіл: деякі компанії звітують виручку під
`RevenueFromContractWithCustomerExcludingAssessedTax` замість
`Revenues` — якщо основний тег дає 404, revenue для цієї компанії
просто не збереться (без падіння адаптера), автоматичного фолбеку
поки нема.

Ще не покрито (майбутнє розширення, не зараз): Earnings (факт vs.
консенсус), Insider trading (Form 4), аналітичні рейтинги/price
targets (Finnhub) — потребують окремого джерела понад сирі XBRL-факти.

## Ринок загалом

- S&P 500, Nasdaq — індекси як бенчмарк
- VIX — індекс волатильності ("індикатор страху")
- Крива дохідності — вже покрито вище (DGS10/DGS2)

## Крипто (BTC/ETH/SOL, docs/watchlist.md) — ✅ адаптери написані, 2026-09-26

| metric_id | Джерело | Що |
|---|---|---|
| `btc_close` / `eth_close` / `sol_close` | `crypto/binance_adapter.py` | щоденний close (klines, проти USDT) |
| `btc_volume` / `eth_volume` / `sol_volume` | `crypto/binance_adapter.py` | щоденний обсяг торгів (klines) |
| `btc_market_cap` / `eth_market_cap` / `sol_market_cap` | `crypto/coingecko_adapter.py` | щоденний market cap (агрегатор, чого немає в даних однієї біржі) |

`--source binance`/`--source coingecko` обов'язковий для цих
metric_id (обидва джерела використовують ті самі btc/eth/sol —
`_resolve_source()` у `run_collect.py` навмисно вимагає явний вибір,
не вгадує).

Funding rates, on-chain метрики — опційно, пізніше і складніше, не
зараз.

## Срібло (XAG/USD) — ✅ через CoinGecko-проксі, 2026-09-26

`crypto/coingecko_adapter.py`, `metric_id` `xagusd_close`, coin_id
`kinesis-silver` (KAG — токен Kinesis Money, 1:1 до фізичного срібла
в алокованому сховищі). Причина проксі, не прямого джерела —
Twelve Data (`quotes/twelvedata_adapter.py`, той самий шлях, що для
золота `xauusd`) вимагає платний тариф для XAG/USD. Санітарна
перевірка (gold:silver ratio) і застереження — `docs/decisions.md`,
2026-09-26.

## Новини / сентимент (джерело: GDELT + офіційні RSS)

- Окремий потік, фільтрація за релевантністю — деталі в Фазі 1 плану

## Календар релізів

Коли виходять наступні дані по кожному показнику вище. Критично для
Фази 3 (моніторинг), але структуру варто продумати вже під час Фази 1,
бо вона впливає на схему `release_log` (вже є заготовка в `db/schema.sql`).

---
**Наступний крок (Фаза 1):** Макро (США, єврозона, Японія) повністю
закрито й підтверджено живими прогонами. Скринінг акцій: universe —
S&P 500, критерії — `config.py`. `quotes/twelvedata_adapter.py`
готовий, підтверджено живим запитом реальних даних (Alpaca і Stooq
відкинуто — гео-блок і бот-захист відповідно, `docs/decisions.md`).
Далі — прогін скринера на весь S&P 500 → конкретний список ~10
тикерів → `companies/` (SEC EDGAR), генерична реалізація з `METRICS`-
словником концептів (тикер — параметр, той самий підхід, що й у
Twelve Data). Китай/Індія — поза планом (рішення користувача
2026-08-30).
