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

Кожна з 48 джоб має однойменну команду бота — повний список з описами
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

## Розклад

**48 джоб**, часовий пояс `Europe/Kyiv`. Єдине місце для зміни
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
| 4:30 / 4:35 | прибирання логів (>14 днів) і новин (>48 год) |
| 5:00 / 5:10 | скринінг акцій → звіт |
| 6:00 | календар релізів, ціни watchlist, синтез новин |
| 7:00 | огляд календаря (пн-пт), список для торгів |
| 8:20 / 9:20 / 9:50 | синтез ринку: Європа / Азія / США |
| 14:00 | розгорнутий аналіз активів (пн-пт) |
| 15:20 / 15:35 | денні макро-серії → прогноз (пн-пт) |
| 16:00 / 16:30 | звірка FRED↔BLS, синтез ставки |
| 17:00 | ціни watchlist (вечірній зріз) |
| 20:00 | щоденний дайджест |
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

Що зростає й чи можна чистити:

| Таблиця | Зростання | Чистити? |
|---|---|---|
| `raw_observations` | ~100–200 МБ/міс | **ніколи** (правило 4) |
| `llm_call_log` | ~10–30 МБ/міс | можна, старші за кілька місяців — але спершу знайте, що втрачаєте аудит |
| `raw_news` | стабільно ~3 МБ | само, 48 год |
| решта | десятки МБ | само або незначне |

Якщо диск тисне, перший кандидат — старі рядки `llm_call_log`, **на
які ніхто не посилається**:

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c "
  delete from llm_call_log l
   where l.created_at < now() - interval '90 days'
     and not exists (select 1 from news_synthesis        x where x.llm_call_id=l.id)
     and not exists (select 1 from market_synthesis      x where x.llm_call_id=l.id)
     and not exists (select 1 from candidate_assets      x where x.llm_call_id=l.id)
     and not exists (select 1 from expectation_synthesis x where x.llm_call_id=l.id)
     and not exists (select 1 from metric_forecasts      x where x.llm_call_id=l.id)
     and not exists (select 1 from news_consolidated     x where x.llm_call_id=l.id)
     and not exists (select 1 from calendar_outlook      x where x.llm_call_id=l.id)
     and not exists (select 1 from fundamental_analysis  x where x.llm_call_id=l.id)
     and not exists (select 1 from news_analysis         x where x.llm_call_id=l.id)"
```

`raw_observations` не чистити **ніколи** — навіть якщо джерело
змінилось або зникло. Тільки архівувати (резервом).

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
це і є дефект, навіть якщо всі 1354 тести зелені.
