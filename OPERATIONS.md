# Експлуатація

Щоденне керування вже працюючим агентом. Розгортання — у
[DEPLOYMENT.md](DEPLOYMENT.md), розбір збоїв — у
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Щоденна перевірка живучості

Агент самостійний, але варто знати три команди, які відповідають на
питання «він узагалі живий?».

### 1. Контейнери

```bash
docker compose ps
```

Три сервіси, усі `running`, `db` ще й `(healthy)`. Якщо `scheduler`
перезапускається по колу — дивіться його логи.

### 2. Пульс планувальника

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select * from scheduler_heartbeat"
```

Джоба `scheduler_heartbeat` пише сюди кожні 5 хвилин. Мітка старша за
~10 хвилин означає, що планувальник стоїть, **навіть якщо контейнер
`running`** — саме для цього діагностика й існує.

### 3. Чи приходять дані

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select source, count(*), max(fetched_at) as last_fetch
     from raw_observations group by source order by 3 desc"
```

У джерел із денним циклом (`binance`, `twelvedata`, `yahoo_chart`)
`last_fetch` мусить бути за сьогодні. У макро-джерел (`fred`, `ecb`,
`boj`, `estat`) — ні: там природний цикл релізу місяць-квартал, і це
**не** застарілість.

### 4. Чи тригерились релізи

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select metric_id, scheduled_at, status
     from release_log order by scheduled_at desc limit 12"
```

Порожня таблиця на свіжому розгортанні = не виконано крок 7 з
[DEPLOYMENT.md](DEPLOYMENT.md) (`refresh_calendar`).

---

## Самоконтроль агента (з 2026-10-15)

Три механізми, які агент застосовує до СЕБЕ. Усі троє мовчать, коли
все гаразд — бо перевірка, що щодня звітує «усе добре», стає шумом і
її перестають читати.

### Чи виводи не виродились — `output_health`, щодня 22:30

Домінантний клас дефектів цього агента — не падіння, а виродження:
джоба відпрацювала, рядки записала, і вони нічого не значать (був
живий випадок, коли одне поле дорівнювало 8 у ВСІХ рядках, бо пошук
дублікатів не знаходив їх узагалі). Перевірка міряє кількість рядків,
**різноманіття значень** і частку порожніх обов'язкових полів по 7
таблицях.

Telegram приходить **лише при зміні стану** (ok→fail або fail→ok).
Подивитись історію:

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select check_name, status, detail, checked_at
     from output_health_checks order by checked_at desc limit 14"
```

Після свіжого розгортання кілька перевірок можуть один раз сказати
`fail` із текстом «джоба перестала писати» — це означає лише, що
відповідні джоби ще не відпрацювали у своєму вікні. Наступного дня
прийде «✅ Відновилось».

### Чи агент був правий — `evaluate_outcomes` + `notify_outcomes`

Щодня о 06:05 агент бере свої рішення, старші за 7 днів, дивиться
фактичний рух активу й ставить вердикт. Щопонеділка о 09:30 надсилає
відсоток влучань по видах списків.

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select decision_kind,
          count(*) filter (where verdict='correct')   as correct,
          count(*) filter (where verdict='incorrect') as incorrect,
          round(avg(realized_pct) filter (where verdict in ('correct','incorrect')),2) as avg_move
     from decision_outcomes group by 1 order by 1"
```

**Як це читати.** Рух менший за ±2% і спостереження без напрямку
(`watch`/`unclear`) у відсоток не входять — інакше hit rate показував
би ~50% на шумі. Одне вікно — це один режим ринку: у падаючому тижні
напрямок «вгору» програє незалежно від якості відбору, тому дивитись
варто ДИНАМІКУ між тижнями, а не абсолютне число. Це вимір якості
інформації, не торгівлі: ні позицій, ні цін виконання, ні P&L тут
немає.

### Чи ціни правдиві — `quotes_crosscheck`, щодня 05:20

Котирування топових за скором акцій звіряються з Yahoo, а BTC/ETH/SOL —
із CoinGecko, **за ту саму дату**. Сирих даних не змінює (правило
append-only), сповіщає лише при розбіжності понад допуск. Привід, через
який це з'явилось: одне офіційне джерело колись давало ціну Brent на
33% вище за справжню, і контролі свіжості цього не бачили — вони
відповідають про ЧАС, не про ПРАВИЛЬНІСТЬ.

---

## Резервне копіювання

### Зняти резерв

```bash
bash scripts/backup_db.sh                        # у db/backup/ (для коміту)
bash scripts/backup_db.sh ~/резерв-2026-10-13.sql.gz
```

Скрипт друкує таблицю «у базі / у резерві» і **падає при
розбіжності**. Це не формальність: саме цей гейт зловив власний
дефект скрипта — `pg_dump` по hypertable `raw_observations` віддавав
порожнього батька, і «успішний» резерв на 1.8 МБ містив **0** рядків
замість 340 тисяч. Побачили `❌` у звіті — резерв непридатний,
розгортати з нього не можна.

### Що входить у резерв і чому

| Входить | Чому |
|---|---|
| `raw_observations` (~340 тис.) | **незамінне.** Append-only історія, на якій тримаються прогнози. Перезбір із нуля її не відтворить |
| `backtest_verdicts` | на них `method_selector` вирішує ШІ vs naive. Порожні = агент молча падає на naive по всіх 19 показниках |
| `watchlist_assets` | налаштовано людиною через Telegram, у коді цього немає |
| `sources`, `release_log`, результати аналізу | щоб нове розгортання одразу мало що показати |
| `llm_call_log` — **частково** | лише рядки, на які посилаються включені результати (~300 із ~5400): правило 5 вимагає, щоб під кожним висновком лишався його промпт |
| `notify_fingerprints` | без них нове розгортання повторно розсилає вже надіслане |

| Не входить | Чому |
|---|---|
| `raw_news` | за дизайном живе 48г — стаття тижневої давності прогноз не покращує |
| `news_analysis` | `raw_news_id` має `NOT NULL` + `ON DELETE CASCADE`, тобто зникає разом із новинами |
| `llm_call_log` (решта) | 12 МБ аудиту минулих викликів; нові пишуться самі |
| `api_rate_limit`, `scheduler_heartbeat`, `telegram_command_offset` | раннтайм-стан конкретної машини. Перенесений offset змусив би бота або пропустити команди, або переграти старі |

**Схема в резерв не входить.** Її джерело істини — `db/schema.sql`.
Причина: дамп цілої TimescaleDB-бази тягне внутрішній каталог
`_timescaledb_catalog` і вимагає `timescaledb_pre_restore()` /
`post_restore()` при відновленні — ламається при найменшій
розбіжності версій розширення між машинами. Дані ж через `COPY` в
hypertable укладаються самі, чанки створює TimescaleDB — жодної
привʼязки до версії.

### Резерв за розкладом

Агент сам резервів не робить — це свідомо поза його контуром.
Найпростіше через cron на хості:

```bash
crontab -e
```

```cron
# Щонедільний резерв о 3:00, зберігати 8 останніх
0 3 * * 0 cd /home/$USER/ai-invest-agent && bash scripts/backup_db.sh ~/backups/invest-$(date +\%Y\%m\%d).sql.gz >> ~/backups/backup.log 2>&1 && ls -t ~/backups/invest-*.sql.gz | tail -n +9 | xargs -r rm
```

Резерв знімається з живої бази без зупинки агента (`COPY` читає
консистентний знімок).

### Відновитися з резерву

```bash
docker compose down -v           # ⚠ ВИДАЛЯЄ volume бази і всі поточні дані
docker compose up -d --build
bash scripts/restore_db.sh ~/резерв-2026-10-13.sql.gz
```

---

## Керування через Telegram

Кожна з 54 джоб має однойменну команду бота — повний список з описами
видає `/help`. Найуживаніші:

| Команда | Що робить |
|---|---|
| `/help` | список усіх команд з описом |
| `/daily_digest` | дайджест найважливішого за добу |
| `/notify_watchlist` | поточні ціни watchlist-активів |
| `/notify_screening` | скринінг S&P 500 |
| `/notify_trading_list` | список активів для розгляду |
| `/notify_market_synthesis` | стан ринку (сесійний синтез) |
| `/notify_policy_rate` | прогноз рішення по ставці |
| `/notify_candidates` | нові кандидати-новачки |
| `/notify_crypto_screening` | крипто-кандидати SHORT/WATCH |
| `/notify_crypto_long` | крипто-кандидати LONG |

### Редагування watchlist

```
/watchlist_list
/watchlist_add BNB
/watchlist_add GBP/USD
/watchlist_remove gbpusd
```

Агент пробує Twelve Data, потім Binance spot на тому самому тикері —
не вгадує джерело за форматом рядка. Нові FRED-серії й CoinGecko-метрики
й далі вимагають коду.

Якщо «/»-меню бота порожнє:

```bash
docker compose exec app python orchestration/register_telegram_commands.py
```

---

## Пряме спілкування з агентом (з 2026-10-15)

Напиши в чат бота **просто текстом** — або `/ask <питання>`. Агент
відповідає ЛИШЕ зі своїх даних і окремим рядком каже, чого йому
бракувало.

```
що зараз із золотом і чому?
що по BX, чому він у списку?
а ти часто вгадуєш? на чому найгірше?
```

Що потрапляє в контекст відповіді: ціна й зміна за місяць, місце в
списку торгів із розкладкою скору, синтез ціна/новини, консолідовані
новини по активу, найближчі релізи, дата звіту компанії та **зміряна
якість минулих рішень** по цьому активу.

**Межі, про які варто знати:**
- відповідь будується з НАШИХ таблиць; чого в них немає — буде в
  рядку «Бракує даних», а не домислено;
- 15 секунд між питаннями й 60 питань на добу (кожне — платний виклик
  LLM); репліки коротші за 8 символів ігноруються;
- вказівок «купуй/продавай» агент не дає за побудовою — описує стан,
  відхилення й ризики;
- текст новин усередині контексту позначений як дані: спроба вкладеної
  інструкції в новині не виконується (перевірено атакою — модель ще й
  попереджає про неї).

Журнал усіх питань і відповідей — у `llm_call_log` з
`purpose = 'assistant_answer'`, разом із повним контекстом, на якому
стояла відповідь:

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select created_at, left(response, 200) from llm_call_log
    where purpose = 'assistant_answer' order by created_at desc limit 5"
```

---

## Розклад

**54 джоби**, часовий пояс `Europe/Kyiv`. Єдине місце для зміни
періодичності — `orchestration/schedule.py` (запис джоби може мати
власний `timezone` — для привʼязки до відкриття конкретного ринку).
Що саме робить кожна — [docs/jobs.md](docs/jobs.md).

```bash
docker compose exec app python orchestration/run_job.py --list
```

Орієнтир по добі:

| Час | Що відбувається |
|---|---|
| щохвилини | `release_pipeline` (релізний конвеєр із дешевим гейтом), `telegram_commands` |
| кожні 5 хв | `scheduler_heartbeat` |
| кожні 2 год | збір новин з RSS |
| 0/6/12/18 | новини watchlist, аналіз новин, крипто-скринінг |
| 3:00 | ціни всього S&P 500 |
| 4:30 / 4:35 / 4:40 | прибирання логів (>14 днів), новин (>48 год) і похідних таблиць |
| 4:50 | календар звітності компаній (Nasdaq) |
| 5:00 / 5:10 | скринінг акцій → звіт |
| 5:20 | звірка котирувань із другим джерелом |
| 6:05 | журнал наслідків: що сталось із рішеннями агента |
| 6:00 | календар релізів, ціни watchlist, синтез новин |
| 7:00 | огляд календаря (пн-пт), список для торгів |
| 8:20 / 9:20 / 9:50 | синтез ринку: Європа / Азія / США |
| 14:00 | розгорнутий аналіз активів (пн-пт) |
| 15:20 / 15:35 | денні макро-серії → прогноз (пн-пт) |
| 16:00 / 16:30 | звірка FRED↔BLS, синтез ставки |
| 17:00 | ціни watchlist (вечірній зріз) |
| 20:00 | щоденний дайджест |
| 22:30 | детектор вироджених виводів (Telegram лише при зміні стану) |
| пн 9:30 | відсоток влучань рішень за тиждень |
| нд 8:00 | фундаментал усього S&P 500 |
| сб 9:00 | тези зі звітів 10-K/10-Q |
| 1-го числа 7:00 | страховий перезбір усіх показників |

---

## Ручний запуск

```bash
docker compose exec app python orchestration/run_job.py --list
docker compose exec app python orchestration/run_job.py release_pipeline
```

Той самий код і образ, що в планувальника — не «схожий на».

Окремі скрипти напряму (повний перелік — `docs/jobs.md`):

```bash
docker compose exec app python data-ingestion/run_collect.py --metric cpi
docker compose exec app python analysis/forecasting/forecast_metric.py --metric cpi
docker compose exec app python analysis/trading_list/run_trading_list.py --dry-run
docker compose exec app python reporting/telegram_notify.py --metric cpi
```

`--dry-run`, де є, друкує промпт або результат **без** виклику LLM —
безкоштовно, саме для перевірки.

---

## Логи

```bash
docker compose logs -f scheduler        # планувальник: старт, перелік джоб, падіння
docker compose logs --tail 100 app
ls -lt logs/ | head -20                 # вивід окремих джоб (host, у .gitignore)
tail -f logs/release_pipeline_$(date +%Y%m%d).log
```

Файл на джобу на добу, прибираються через 14 днів (`prune_logs`).
Орієнтир норми — **~50 файлів/добу**; якщо їх тисячі, якась джоба
крутиться холостими прогонами.

---

## Моніторинг витрат на LLM

Платних викликів найбільше в механіці (DeepSeek) і найдорожчі в
прогнозі/висновках (Anthropic). Скільки й на що:

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select provider, purpose, count(*), max(created_at)
     from llm_call_log
    where created_at > now() - interval '24 hours'
    group by 1,2 order by 3 desc"
```

Маршрутизація викликів — `config.py`, розділ `LLM_ROUTES`. Аварійний
важіль на випадок, що скінчився бюджет або провайдер лежить:
`SYNTHESIS_LLM_PROVIDER=deepseek` у `.env` переводить **усі** маршрути
на дешевого провайдера (`anthropic` — навпаки). Порожнє значення =
нормальний режим.

---

## Зростання бази

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select pg_size_pretty(pg_database_size('invest_agent'))"
```

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select hypertable_name,
          pg_size_pretty(hypertable_size(format('%I.%I',hypertable_schema,hypertable_name)::regclass))
     from timescaledb_information.hypertables"
```

Що зростає й що з цим робить агент САМ (з 2026-10-15):

| Таблиця | Зростання | Хто прибирає |
|---|---|---|
| `raw_observations` | ~5 200 рядків/добу (ф'ючерси — найбільше) | **ніколи не видаляється.** Компресію пробували 15.10 і **відкотили 16.10** — вона поклала БД (OOM-kill бекендів на плануванні запиту по 512 тикерах). Деталі й умови повторної спроби — `docs/decisions.md`, 2026-10-16 |
| `llm_call_log` | ~10-30 МБ/міс | джоба `prune_derived`: старші за 120 днів і **без посилань** із 11 таблиць-результатів |
| `news_consolidated` | тексти, 3×/добу | `prune_derived`, 30 днів |
| знімкові таблиці (`trading_list`, `screening_results`, …) | десятки МБ | `prune_derived`, 180 днів |
| `raw_news` | стабільно ~3 МБ | `prune_raw_news`, 48 год |

Ручного SQL для цього більше не потрібно. Перевірити, що прибирання
працює:

```bash
docker compose logs --tail 30 scheduler | grep prune_derived
```

Вікна свідомо широкі, тож на свіжій базі джоба видаляє НУЛЬ рядків —
це гейт на майбутнє, не разове прибирання. Пороги — `config.py`,
розділ 24 (`DERIVED_RETENTION_DAYS`).

**Чого прибирання не торкається ніколи:** `raw_observations` (сирі
зібрані дані), `backtest_verdicts` (без них вибір «ШІ чи naive» тихо
падає на naive по всіх показниках), `metric_forecasts` і
`decision_outcomes` (вимірювана історія ЯКОСТІ агента),
`earnings_calendar`, `release_log`, `watchlist_assets`, `sources`.
Перелік стережеться тестом — випадково додати туди таблицю не вийде.

Перевірити, що стиснутих чанків НЕМА (після відкату 16.10 має бути 0):

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select count(*) filter (where is_compressed) as compressed,
          count(*) as chunks
     from timescaledb_information.chunks
    where hypertable_name = 'raw_observations'"
```

Якщо `compressed` більше нуля — запусти
`bash scripts/decompress_raw_observations.sh`: це ознака, що база ще
не відкочена, і джоби скринінгу падатимуть з
`server closed the connection unexpectedly`.

---

## Зупинка й запуск

```bash
docker compose stop            # зупинити, дані зберегти
docker compose start
docker compose restart scheduler
docker compose down            # зняти контейнери, volume з даними ЗАЛИШИТИ
docker compose down -v         # ⚠ зняти ВСЕ, разом із даними
```

Різниця між `down` і `down -v` — єдина справді небезпечна команда тут.
Перед `-v` завжди `bash scripts/backup_db.sh`.

---

## Зміна конфігурації

| Що змінити | Де |
|---|---|
| пороги, ваги, чек-листи, маршрути LLM, чинники активів | `config.py` — **єдиний** конфіг на весь агент |
| періодичність джоб | `orchestration/schedule.py` |
| ключі, пароль БД, часовий пояс | `.env` |
| склад RSS-фідів | `data-ingestion/news/rss_feeds.py` |
| схема БД | `db/schema.sql` + `apply_schema.py` |

Жоден модуль не визначає порогів сам — усі імпортують із `config.py`.
Єдиний виняток — `orchestration/schedule.py`: це **розклад**, не
пороги.

Після зміни `config.py` або `schedule.py`:

```bash
docker compose restart scheduler
```

Ребілд (`docker compose up -d --build`) потрібен **лише** при зміні
`requirements.txt` або `Dockerfile` — код монтується як volume.

---

## Коли щось змінили в аналітиці

Правило, виведене з усієї розробки: **перевіряти живими даними з БД, а
не тільки тестами, і міряти до та після.**

Тести ловлять зламаний код. Вони **не** ловлять головний клас дефекту
тут — коли функція тихо повертає `False`, порожній список або
константу й **виглядає робочою** (приклади — [README.md](README.md),
принцип 8).

Мінімальний цикл:

```bash
# 1. ДО: зафіксувати поточний вивід
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select asset_id, catalyst_score from trading_list order by created_at desc limit 20" \
  > /tmp/before.txt

# 2. правка + тести
docker compose exec app pytest -q

# 3. живий прогін
docker compose exec app python analysis/trading_list/run_trading_list.py

# 4. ПІСЛЯ: той самий запит і порівняння
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select asset_id, catalyst_score from trading_list order by created_at desc limit 20" \
  > /tmp/after.txt
diff /tmp/before.txt /tmp/after.txt
```

Вивід **не змінився** там, де мусив, — або всі значення **однакові** —
це і є дефект, навіть якщо всі 1503 тести зелені.
