# Усунення проблем

Кожен розділ — реальний збій, який уже траплявся, з точним текстом
помилки. Шукайте по тексту, який бачите.

> **Головне, що треба знати про цей проєкт.** Дефект тут майже ніколи
> не падає з трасуванням. Він тихо повертає `False`, порожній список
> або константу — **і виглядає як робочий код**. Тому розділ
> [«Нічого не падає, але виводи виглядають неправильно»](#нічого-не-падає-але-виводи-виглядають-неправильно)
> не менш важливий за всі розділи з текстами помилок.

---

## Зміст

**Розгортання:** [compose не стартує](#required-variable-db_password-is-missing-a-value) ·
[порт зайнятий](#порт-5432-зайнятий) ·
[відновлення бази](#відновлення-бази) ·
[тести падають](#тести-падають)

**Робота:** [планувальник стоїть](#планувальник-стоїть-а-контейнер-running) ·
[контейнер перезапускається](#контейнер-перезапускається-по-колу) ·
[app помер](#контейнер-app-помер-exited-137) ·
[немає звітів у Telegram](#у-telegram-нічого-не-приходить)

**Дані:** [ключ не задано](#fred_api_key-не-задано) ·
[ForeignKeyViolation](#foreignkeyviolation-key-source-is-not-present-in-table-sources) ·
[дані застарілі](#дані-виглядають-застарілими) ·
[ліміт запитів](#429--rate-limit-exceeded) ·
[GDELT не віддає](#gdelt-не-віддає-новин) ·
[ціни розходяться з терміналом](#значення-розходиться-з-реальним-торговим-терміналом)

**Код:** [ModuleNotFoundError](#modulenotfounderror-no-module-named-config) ·
[зміни не доходять](#зміни-у-файлі-не-доходять-до-контейнера) ·
[тихі дефекти](#нічого-не-падає-але-виводи-виглядають-неправильно)

**Диск і резерви:** [місце](#закінчується-місце-на-диску) ·
[резерв неповний](#резерв-неповний--рядки-з--не-збіглися-з-базою)

---

## Розгортання

### `required variable DB_PASSWORD is missing a value`

```
error while interpolating services.db.environment.POSTGRES_PASSWORD:
required variable DB_PASSWORD is missing a value: Задайте DB_PASSWORD у .env
```

`.env` не існує або `DB_PASSWORD` у ньому порожній. Це **навмисна**
зупинка: база без пароля не підніметься.

```bash
cp .env.example .env && nano .env     # заповнити DB_PASSWORD
```

`.env` мусить лежати в тій самій теці, що `docker-compose.yml`.
Наявності `.env.example` недостатньо.

### Порт 5432 зайнятий

```
Error starting userland proxy: listen tcp4 127.0.0.1:5432: bind: address already in use
```

На хості вже є PostgreSQL. Або зупиніть його
(`sudo systemctl stop postgresql`), або дайте агенту інший порт у
`.env`:

```
DB_PORT=5433
```

Усередині docker-мережі це нічого не змінює: `docker-compose.yml`
перекриває для `app` і `scheduler` **і** `DB_HOST: db`, **і**
`DB_PORT: 5432` — тобто `DB_PORT` із `.env` впливає лише на порт
публікації на хост.

> Так було не завжди. До 2026-10-13 перекривався тільки `DB_HOST`, і
> `DB_PORT=5433` просочувався в `common/db.py:get_connection()`:
> кожна джоба падала з `connection to server at "db" (172.19.0.2),
> port 5433 failed: Connection refused`. Тобто звичайний обхід
> конфлікту портів ламав агента цілком, а причина виглядала як
> проблема з базою. Знайдено наскрізним тестом розгортання. Якщо
> бачите цю помилку — у вас версія до виправлення; перевірте, що в
> `docker-compose.yml` є обидва рядки.

### Відновлення бази

#### `❌ У raw_observations уже N рядків — база не порожня`

Гейт `restore_db.sh`. Резерв містить `COPY`, а не upsert: накладання на
наповнену базу або впаде на унікальних обмеженнях, або **подвоїть
історію спостережень**. Тихе подвоєння гірше за падіння.

Якщо дійсно треба начисто:

```bash
bash scripts/backup_db.sh ~/на-всяк-випадок.sql.gz
docker compose down -v && docker compose up -d --build
bash scripts/restore_db.sh
```

#### `duplicate key value violates unique constraint "sources_pkey"`

Резерв накладається на базу, де вже застосовано схему **і** її
seed-рядки (`db/schema.sql` засіває `sources` і `watchlist_assets`).
Актуальні резерви чистять сід самі. Якщо бачите це — резерв знятий
старою версією `backup_db.sh`. Перезнімайте актуальною, або вручну
перед завантаженням:

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "set session_replication_role=replica;
   delete from watchlist_assets; delete from sources;"
```

#### `raw_observations` = 0 після відновлення

Резерв порожній по головній таблиці. Майже завжди — резерв, знятий
`pg_dump -t public.raw_observations` **без** урахування того, що це
hypertable: сама таблиця порожня, усі рядки лежать у чанках
`_timescaledb_internal._hyper_1_*`, і `pg_dump` віддає порожнього
батька, не повідомляючи про це.

Актуальний `backup_db.sh` робить це правильно (`COPY` через батька) і
**падає**, якщо кількість рядків не збіглася. Перезнімайте резерв ним.

#### `relation "raw_observations" does not exist`

Схему не застосовано. `restore_db.sh` робить це сам; при ручному
відновленні — спершу:

```bash
docker compose exec app python data-ingestion/apply_schema.py
```

### Тести падають

```bash
docker compose exec app pytest -q                      # очікується 1515 passed
docker compose exec app pytest -q --tb=short 2>&1 | tail -40
docker compose exec app pytest analysis/tests/test_trend.py -x -vv
```

Тести не торкаються мережі й бази — лише код, і **не потребують
жодного API-ключа**: усі 1515 проходять на незаповненому `.env`
(перевірено наскрізним розгортанням 2026-10-13). Якщо вони падають
одразу після `git clone`, найчастіша причина — старий образ:

```bash
docker compose up -d --build
```

> Якщо падають рівно ці чотири:
> `test_consolidate_stream_uses_mechanical_source_count_not_llm_count`,
> `test_consolidate_stream_skips_items_duplicating_recent_summaries`,
> `test_merge_stream_merges_and_returns_removed_count`,
> `test_merge_stream_noop_when_llm_finds_no_duplicates`
> — з `LlmError: DEEPSEEK_API_KEY не задано`, то у вас версія до
> 2026-10-13. Вони читали **справжнє** середовище замість мокати ключ:
> `llm_common._call_provider` перевіряє змінну **до** виклику
> провайдера, тож підміна `call_deepseek` у тесті не досягалась. У
> розробці це не було видно (там ключ є), а на свіжому розгортанні
> виглядало так, ніби зламаний код. Виправлено
> `monkeypatch.setenv` у кожному з чотирьох.

Тести, які проходять, кажуть **лише** що код цілий. Вони не
перевіряють наявність даних: на порожній базі всі 1515 пройдуть, а
скринінг і синтез нічого не покажуть.

---

## Робота

### Планувальник стоїть, а контейнер `running`

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select * from scheduler_heartbeat"
```

Мітка старша за ~10 хвилин = планувальник не працює, хоч контейнер і
живий. Саме для цього heartbeat існує — «контейнер `running`» нічого
не доводить.

```bash
docker compose logs --tail 100 scheduler
docker compose restart scheduler
```

Агент і сам попереджає про це: при старті, якщо розрив з останнім
heartbeat перевищив поріг, у Telegram приходить алерт — щоб ви знали
причину затримки звітів **заздалегідь**, а не дивувались «чому так
пізно».

### Контейнер перезапускається по колу

```bash
docker compose logs --tail 200 scheduler
```

Типові причини, у порядку частоти:

1. **Немає обов'язкової змінної** в `.env` — у логах буде `KeyError`
   або «не задано».
2. **База ще не готова** — має лікуватися `depends_on: service_healthy`
   автоматично; якщо ні, `docker compose restart db` і подивіться
   `docker compose logs db`.
3. **Помилка імпорту** після `git pull` без ребілду — див.
   [ModuleNotFoundError](#modulenotfounderror-no-module-named-config).

### Контейнер `app` помер (`Exited (137)`)

```
app   exited   Exited (137) 2 hours ago
```

137 = SIGKILL. Найчастіше — OOM killer хоста: `app` виконує важкі
джоби (масовий збір, pytest), і на машині з 2 ГБ RAM цього буває
достатньо.

```bash
docker compose up -d app                 # підняти знову
docker inspect ai-invest-agent-app-1 --format '{{.State.OOMKilled}}'
dmesg | grep -i "killed process" | tail -5
```

`OOMKilled: true` або рядок у `dmesg` підтверджують діагноз. Що
робити: додати swap, або не запускати масові збори одночасно з
`pytest`.

**Важливо:** смерть `app` **не** зупиняє агента. Джоби виконує
`scheduler` — окремий контейнер. `app` потрібен лише для ручних
команд (`docker compose exec app ...`), і без нього вони падають з
`service "app" is not running`.

### У Telegram нічого не приходить

По черзі:

```bash
# 1. Ключі задані?
docker compose exec app python -c \
  "import os; print('TOKEN', bool(os.environ.get('TELEGRAM_BOT_TOKEN')), 'CHAT', bool(os.environ.get('TELEGRAM_CHAT_ID')))"

# 2. Канал узагалі працює?
docker compose exec app python orchestration/run_job.py notify_watchlist

# 3. Що сказала джоба?
tail -50 logs/notify_watchlist_$(date +%Y%m%d).log
```

Якщо ключів немає — агент це **не** вважає збоєм: він далі збирає й
пише в базу, лише логує, що алерт не надіслано.

Якщо канал працює, а конкретного звіту немає — найімовірніше **нічого
не пройшло фільтр** (жоден актив не набрав порогу, жодна новина не
визнана релевантною). Це нормальна робота, не збій. Перевірити, чи
взагалі є що показувати:

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select count(*), max(created_at) from trading_list"
```

Порожнє «/»-меню бота:

```bash
docker compose exec app python orchestration/register_telegram_commands.py
```

---

## Дані

### `FRED_API_KEY не задано`

`.env` існує (не лише `.env.example`) і ключ реально вписаний? Після
правки `.env`:

```bash
docker compose up -d        # перечитує env_file
```

`docker compose restart` **не** перечитує `.env` надійно — потрібен
`up -d`.

### `ForeignKeyViolation: Key (source)=(...) is not present in table "sources"`

Джерело не зареєстроване в seed-даних `db/schema.sql`.

```bash
docker compose exec app python data-ingestion/apply_schema.py
```

Якщо не допомогло — ви на версії коду, новішій за схему в базі.
Перевірте, що `git pull` і `apply_schema.py` виконані обидва.

### Дані виглядають застарілими

**Спершу відділіть два різні випадки.**

**Випадок 1: показник місячний/квартальний за природою.** ВВП, CPI,
PCE, nonfarm виходять раз на місяць — «застарілість» на 3 тижні тут
нормальна, це цикл релізу самого показника. Не збій.

**Випадок 2: денний актив справді стоїть.** Ціна акції, крипти,
валютної пари мусить бути за сьогодні.

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select source, metric_id, max(observed_at) obs, max(fetched_at) fetch
     from raw_observations
    where source in ('binance','twelvedata','yahoo_chart','coingecko')
    group by 1,2 order by 3 limit 20"
```

Причини, у порядку частоти:

1. **Джоба не виконалась** — дивіться `scheduler_heartbeat` і логи.
2. **Зовнішнє джерело змінило датасет** без помилки в запиті. Так
   робить ECB: закриває старий датасет і переносить на новий, запит
   формально успішний, даних немає (живий кейс — міграція ICP→HICP,
   `docs/decisions.md`). Перевірте офіційну дату останнього релізу
   показника і порівняйте з тим, що віддав запит.
3. **Ліміт запитів** — див. нижче.

### `429` / `Rate limit exceeded`

Найчастіше Twelve Data: free tier ~8 запитів/хв, 800/добу, а в
S&P 500 ~500 тикерів. Агент тримає власний облік лімітів
(`data-ingestion/common/rate_limit.py`, таблиця `api_rate_limit`) і
сам розтягує збір — тому `quotes_universe_refresh` триває довго, це
нормально.

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select * from api_rate_limit"
```

Якщо ліміт вичерпано вручними прогонами — просто почекайте до
наступної доби.

### GDELT не віддає новин

GDELT — неофіційне джерело й може надовго відправити IP у штрафний
стан. Це вже траплялось: джоба падала щопрогону, і через це
**geopolitical і general потоки переведено на RSS** (17 фідів, ~140
статей/добу). GDELT лишився тільки для цільових запитів по watchlist
і тикерах зі скринінгу.

Тобто падіння GDELT більше не зупиняє новинний конвеєр. Перевірити,
що RSS працює:

```bash
docker compose exec app python orchestration/run_job.py news_collect_rss
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select stream, count(*), max(published_at)
     from raw_news group by 1"
```

### Значення розходиться з реальним торговим терміналом

**Це найсерйозніший клас проблеми тут, і він не падає сам.**

Живий кейс: FRED — офіційне першоджерело — віддавав Brent за
**135.51** проти справжніх **102.25** (розбіжність 33%). Обидва наші
контролі казали «все гаразд», бо `freshness.py` і `quality.py`
відповідають на питання про **час**, не про **правильність**. Хибне
значення було свіже.

Саме тому в проєкті діє правило: **правда — це згода кількох джерел, а
не офіційність одного**.

```bash
# Що каже звірка джерел
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select source, metric_id, value, observed_at
     from raw_observations
    where metric_id = 'brent_crude'
      and observed_at > now() - interval '7 days'
    order by observed_at desc, source"
```

Два джерела на один актив розходяться — вірте **не** офіційнішому, а
тому, що збігається з третім і з терміналом. Механізм звірки —
`data-ingestion/common/consensus.py`.

При додаванні або заміні джерела перевіряйте це **одразу**, а не
постфактум: чи значення збігається з незалежним джерелом, чи його не
переписують заднім числом, чи досить історії для розрахунків на ньому
(напр. `synthesize.py` потребує 2+ точок).

**І ще одна пастка звірки:** брати можна **лише той самий різновид
ряду**. `CUUR0000SA0` проти FRED `CPIAUCSL` дає розбіжність 0.25%, яка
виглядає як знахідка — а насправді перший ряд **без** сезонного
згладжування, другий **зі**. Правильна пара — `CUSR0000SA0`, і вона
дає точний збіг. Інакше шар звірки сипав би хибну тривогу щодня й
знецінився б.

---

## Код

### `ModuleNotFoundError: No module named 'config'`

Корінь репозиторію мусить бути в `PYTHONPATH` — у `Dockerfile` є
`ENV PYTHONPATH=/app`. Якщо помилка все одно є, образ старий:

```bash
docker compose up -d --build
```

Історія цього рядка варта згадки: спершу корінь додавали в `sys.path`
**вручну** в 7 скриптів — і тих, що перевірили. А джоб, які тягнуть
конфіг транзитивно, значно більше. Уночі провалилось 7 джоб.
Поскриптовий `sys.path` покриває лише ті входи, про які згадали; тому
тепер це змінна середовища.

### Зміни у файлі не доходять до контейнера

Код монтується як volume (`.:/app`) — правка на хості видна всередині
**одразу**. Якщо ні, майже напевно у вас **Docker Desktop**, а не
нативний Engine: Desktop піднімає контейнери у власній ВМ, і bind
mount синхронізується не миттєво. Живий наслідок: `apply_schema.py`
молча застосовував **застарілу схему**.

```bash
docker info 2>/dev/null | grep -i "operating system\|docker desktop"
```

Лікується переходом на нативний `docker-ce` ([DEPLOYMENT.md](DEPLOYMENT.md)).
Разовий обхід: `docker compose restart app`.

Ребілд (`up -d --build`) потрібен **лише** при зміні `requirements.txt`
або `Dockerfile`.

### Нічого не падає, але виводи виглядають неправильно

Головний клас дефекту в цьому проєкті. Усі шість прикладів нижче
виглядали робочим кодом і були знайдені **виміром виводу**, а не
читанням:

| Симптом | Що було насправді |
|---|---|
| усі активи з однаковим або майже однаковим скором | знаменник нормування давав глобальній події більше ваги, ніж власній новині активу — тобто подія, що **не різнить** активи, важила більше |
| прапорець (ревізій, аномалій) стоїть у всіх | порівнювались рядки однієї дати без урахування часу забирання, а джерело пише 24 внутрішньоденні зрізи |
| ранжування «за підтвердженістю» не ранжує | метрика підтвердженості дорівнювала 1 у **всіх** рядках — сортувались однакові числа |
| детектор завжди повертає «ні» | читав хардкод джерел замість живого списку → отримував ряд із 6 точок → порогу не досягав ніколи |
| дублікати в журналі релізів щопрогону | японський реліз 08:30 JST = 23:30 UTC попереднього дня; дати не збігались, дедуп не бачив дубля |
| тисячі файлів логів на добу | гейт конвеєра не дублював фільтри notify-скриптів; один рядок `impact='low'` тримав його відкритим вічно |

**Як шукати.** Не читати код, а **міряти** — і до, і після:

```bash
# Виводи вироджені? (однакові значення в усіх рядках = дефект)
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select count(*) rows, count(distinct catalyst_score) distinct_scores,
          min(catalyst_score), max(catalyst_score)
     from trading_list where created_at > now() - interval '7 days'"
```

`distinct_scores = 1` при `rows = 300` — дефект, навіть якщо всі 1515
тести зелені.

Повний цикл перевірки правки — [OPERATIONS.md](OPERATIONS.md), розділ
«Коли щось змінили в аналітиці».

---

## Диск і резерви

### Закінчується місце на диску

```bash
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select pg_size_pretty(pg_database_size('invest_agent'))"
du -sh logs/
docker system df
```

У порядку безпечності:

1. **Образи й кеш Docker** — зазвичай найбільший і найдешевший виграш:
   ```bash
   docker image prune -a && docker builder prune
   ```
2. **Логи** — самоочищаються через 14 днів; форсувати:
   ```bash
   docker compose exec app python orchestration/run_job.py prune_logs
   ```
3. **Старий `llm_call_log`**, на який ніхто не посилається — запит у
   [OPERATIONS.md](OPERATIONS.md), «Зростання бази».
4. **`raw_observations` — не чистити ніколи.** Навіть якщо джерело
   змінилось або зникло. Тільки архівувати резервом.

### `❌ Резерв НЕПОВНИЙ — рядки з ❌ не збіглися з базою`

Гейт `backup_db.sh`: кількість рядків у резерві не збіглася з живою
базою. Файл лишається на диску для розбору, але **розгортати з нього
не можна**.

Що перевірити:

```bash
# 1. Не йшов збір просто під час резерву? (тоді число живої бази встигло вирости)
bash scripts/backup_db.sh         # просто перезняти

# 2. Чи не додалася нова таблиця, якої немає в списку TABLES скрипта
docker compose exec db psql -U invest_agent -d invest_agent -c \
  "select relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
    where n.nspname='public' and c.relkind='r' order by 1"
```

Другий випадок — справжня причина, якщо повтор не допомагає: нова
таблиця додана в `db/schema.sql`, але не в список `TABLES` у
`scripts/backup_db.sh`. Там же, у шапці скрипта, описано, що
навмисно виключене й чому.

`llm_call_log` у резерві **навмисно** вужчий за живий (лише рядки, на
які посилаються включені результати) — для нього гейт перевіряє не
рівність, а «не порожньо». Розбіжність у його рядку — не помилка.

---

## Коли нічого не допомогло

Зберіть зліпок стану — з ним розбиратися вдесятеро швидше:

```bash
{
  echo "=== ps ===";        docker compose ps -a
  echo "=== heartbeat ==="; docker compose exec -T db psql -U invest_agent -d invest_agent -c "select * from scheduler_heartbeat"
  echo "=== дані ===";      docker compose exec -T db psql -U invest_agent -d invest_agent -c "select source, count(*), max(fetched_at) from raw_observations group by 1 order by 3 desc"
  echo "=== релізи ===";    docker compose exec -T db psql -U invest_agent -d invest_agent -c "select metric_id, scheduled_at, status from release_log order by scheduled_at desc limit 12"
  echo "=== scheduler ==="; docker compose logs --tail 150 scheduler
  echo "=== тести ===";     docker compose exec -T app pytest -q 2>&1 | tail -20
} > ~/agent-diag-$(date +%Y%m%d-%H%M).txt
```

Повне перезбирання — остання інстанція. **`-v` видаляє всі зібрані
дані**, тому резерв спершу:

```bash
bash scripts/backup_db.sh ~/перед-перезбиранням.sql.gz
docker compose down -v
docker compose up -d --build
bash scripts/restore_db.sh ~/перед-перезбиранням.sql.gz
docker compose exec app pytest -q
```

Де шукати відповідь на «чому так зроблено» — [docs/decisions.md](docs/decisions.md),
журнал рішень із живими цифрами. Що саме робить кожна джоба —
[docs/jobs.md](docs/jobs.md).
