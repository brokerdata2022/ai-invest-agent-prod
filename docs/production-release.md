# Випуск продакшен-версії

Як оновити й опублікувати `production/` — окремий репозиторій, з
якого агент розгортається на ноут або сервер.

## Як це влаштовано

```
~/Завантажене/ai-invest-agent/          ← РОЗРОБКА
  .git  →  github.com/brokerdata2022/ai-invest-agent        (public)
  CLAUDE.md, PLAN.md, docs/archive/, .claude/, .vscode/, logs/
  scripts/build_production.sh           ← збирає продакшен
  scripts/production-notes/             ← тексти позначок на docs/
  production/                           ← у .gitignore цього репо!
      .git  →  github.com/brokerdata2022/ai-invest-agent-prod  (private)
      README.md DEPLOYMENT.md OPERATIONS.md TROUBLESHOOTING.md
      db/backup/invest_agent_data.sql.gz
      scripts/backup_db.sh  scripts/restore_db.sh
```

**Два репозиторії, не дві гілки.** Причина — резерв БД: гілка жила б у
тій самій історії, і 13-мегабайтний бінарник тягнувся б за кожним
клоном дев-репо, включно з усіма своїми старими версіями. Окремий
репозиторій тримає це осторонь і дає приватність продакшену
незалежно від публічності розробки.

`production/` фізично лежить **усередині** теки розробки, але для git
її не існує: `/production/` є в `.gitignore` дев-репо. Вкладеного
репозиторію (submodule) не виникає.

---

## Разова підготовка: створити репозиторій на GitHub

`gh` на цій машині не встановлений, тому репозиторій створюється в
браузері.

1. [github.com/new](https://github.com/new)
2. **Repository name:** `ai-invest-agent-prod`
3. **Visibility: Private** — обов'язково. У резерві БД лежить ваш
   watchlist, результати аналізу й промпти LLM-викликів. Секретів
   (`.env`) там немає, але це ваша робоча аналітика.
4. **Нічого не додавати** — ні README, ні `.gitignore`, ні ліцензію.
   Інакше перший `push` конфліктуватиме.
5. Create repository.

Прив'язати й опублікувати:

```bash
cd ~/Завантажене/ai-invest-agent/production
git remote add origin git@github.com:brokerdata2022/ai-invest-agent-prod.git
git push -u origin main
```

Якщо SSH-ключа для GitHub немає — через HTTPS (запитає токен замість
пароля; токен: Settings → Developer settings → Personal access tokens →
Fine-grained, доступ `Contents: Read and write` до цього репо):

```bash
git remote add origin https://github.com/brokerdata2022/ai-invest-agent-prod.git
git push -u origin main
```

Перший push — ~14 МБ, переважно резерв БД.

---

## Випуск нової версії

```bash
cd ~/Завантажене/ai-invest-agent

# 1. Переконатися, що код цілий (у дев-репо — той самий код)
docker compose exec app pytest -q

# 2. Зібрати продакшен-копію
bash scripts/build_production.sh              # лише код і docs/
bash scripts/build_production.sh --with-db    # і перезняти резерв БД

# 3. Подивитись, що змінилось
git -C production status
git -C production diff --stat

# 4. Закомітити й опублікувати
git -C production add -A
git -C production commit -m "Опис змін"
git -C production push
```

Збірка сама перевіряє три речі й **падає**, якщо щось не так:

- у продакшен не просочились `.env`, `CLAUDE.md`, `PLAN.md`,
  `docs/archive/`, `__pycache__`, `.vscode/`, `.claude/`;
- код збігається з дев-версією **побайтово** (md5 кожного з 266
  `.py`-файлів) — перевірка сильніша за прогін тестів, бо збірка
  Python-файли не редагує, і будь-яка розбіжність означає, що `rsync`
  чогось не доніс;
- усі обов'язкові файли на місці (`config.py`, `db/schema.sql`,
  документація, скрипти резерву).

---

## Коли перезнімати резерв БД

`--with-db` додає ~13 МБ **нового** бінарника в історію git при
кожному виклику, і стиснений дамп не дельтується — тобто кожна версія
лежить повністю. Десять оновлень = +130 МБ в репозиторії назавжди.

Тому:

| Ситуація | `--with-db`? |
|---|---|
| правка коду, документації, порогів | **ні** |
| змінилась схема БД (`db/schema.sql`) | **так** — інакше резерв не накладеться на нову схему |
| розгортаєте на нову машину й хочете свіжу історію | так |
| планове оновлення | раз на 1–3 місяці достатньо |

Історія в резерві застаріває мʼяко: агент сам добирає пропущене за
розкладом. Старий резерв дає робочого агента з дірою в кілька тижнів
по денних серіях, а не зламаного.

### Якщо репозиторій усе-таки розпух

Переписати історію, лишивши один актуальний коміт:

```bash
cd ~/Завантажене/ai-invest-agent/production
git checkout --orphan fresh
git add -A
git commit -m "Продакшен-версія $(date +%F)"
git branch -D main && git branch -m main
git reflog expire --expire=now --all && git gc --prune=now
git push -f origin main
```

`push -f` перезаписує історію на GitHub. Для продакшен-репо, де
історія не є цінністю (цінність — поточний стан), це нормально.
Переконайтесь лише, що з нього ніде не розгортаються просто зараз.

---

## Чого в продакшені немає і чому

| Прибрано | Чому |
|---|---|
| `logs/` | раннтайм-вивід; у контейнері створюється наново, прибирається джобою `prune_logs` через 14 днів |
| `backup.sql` | застарілий локальний дамп; джерело істини — `db/backup/invest_agent_data.sql.gz` |
| `CLAUDE.md` (корінь + 5 підпапок) | інструкції для сесій розробки |
| `PLAN.md` | план розробки |
| `docs/archive/` | журнали закритих фаз (940 КБ) |
| `.claude/`, `.vscode/` | інструменти й налаштування IDE розробника |
| `.env` | **секрети** |
| `__pycache__/`, `.pytest_cache/` | кеш |

| Лишено | Чому |
|---|---|
| увесь код побайтово | 266 `.py`-файлів |
| 1484 тести + `pytest.ini` | єдиний гейт перевірки після `git clone` на новій машині; без них на сервері нема чим підтвердити, що код цілий |
| `docs/jobs.md`, `decisions.md`, `architecture.md`, `metrics-catalog.md`, `watchlist.md`, `trading-list.md` | операційні знання, потрібні при розборі збою на сервері |
| `scripts/bootstrap_*.sh` | розгортання з порожньої бази (шлях Б) |

Перенесені `docs/` мають позначку, що посилання в них на `CLAUDE.md`,
`PLAN.md` і `docs/archive/` ведуть у дев-репо. Тексти позначок —
`scripts/production-notes/` (там же пояснено, чому вони саме файлами,
а не рядком у скрипті).

---

## Що в резерві БД

Повний розбір — `production/OPERATIONS.md`, розділ «Резервне
копіювання». Коротко:

**Входить:** `raw_observations` (340 тис. рядків — незамінна
append-only історія, ~300 точок на показник), `backtest_verdicts` (на
них `method_selector` вирішує ШІ vs naive), `watchlist_assets`
(налаштовано через Telegram, у коді немає), `sources`, `release_log`,
результати аналізу, `notify_fingerprints`, і 316 рядків
`llm_call_log` — ті, на які посилаються включені результати (правило 5
вимагає, щоб під кожним висновком лишався його промпт).

**Не входить:** `raw_news` і `news_analysis` (обидві 48-годинні за
дизайном, звʼязані `ON DELETE CASCADE`), решта `llm_call_log` (12 МБ
аудиту минулих викликів), раннтайм-стан машини (`scheduler_heartbeat`,
`api_rate_limit`, `telegram_command_offset` — перенесений offset
змусив би бота або пропустити команди, або переграти старі).

**Схема не входить** — її джерело істини `db/schema.sql`. Дамп цілої
TimescaleDB-бази тягне `_timescaledb_catalog` і вимагає
`timescaledb_pre_restore()`/`post_restore()`, що ламається при
розбіжності версій розширення між машинами. Дані ж через `COPY` у
hypertable укладаються самі.

### Три дефекти резерву, знайдені тестовим відновленням

Жоден не падав сам — рівно той клас, про який попереджає `CLAUDE.md`:

1. **`pg_dump -t public.raw_observations` віддавав порожнього
   батька.** Це hypertable: усі рядки лежать у чанках
   `_timescaledb_internal._hyper_1_*`. Резерв вийшов на 1.8 МБ із
   «raw_observations 0» замість 340 795 рядків, і скрипт при цьому
   **не впав**. Тепер `COPY` через батька.
2. **`db/schema.sql` засіває `sources` і `watchlist_assets`**, тож
   `COPY` падав на `sources_pkey`. Тепер сід чиститься перед
   завантаженням: резерв авторитетніший за схему, бо watchlist
   користувач редагує через Telegram, і в базі він інший, ніж у
   seed-рядках.
3. **Список колонок, вписаний вручну, не збігався зі схемою**
   (`unit`, `metadata` не існують). Тепер виводиться з живої бази —
   щоб резерв не міг розійтися зі схемою при `ALTER TABLE`.

Тому `scripts/backup_db.sh` **звіряє кількість рядків** у резерві з
живою базою й падає при розбіжності. Саме цей гейт зловив дефект 1 —
не читання коду.

Відновлення перевірено живими даними: 1210 чанків, 0 зламаних
посилань, **MD5 усіх 340 795 рядків збігається** з дев-базою.
