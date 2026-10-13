#!/usr/bin/env bash
#
# Відновлення бази з резерву: схема (db/schema.sql) + дані
# (db/backup/invest_agent_data.sql.gz).
#
# Запускається ОДИН раз при розгортанні на новій машині, після
# `docker compose up -d`. Ідемпотентний щодо схеми (apply_schema.py
# уся на CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS), але
# НЕ щодо даних: повторний запуск на вже наповненій базі впаде на
# унікальних обмеженнях. Це навмисно — тихо подвоїти історію
# спостережень гірше, ніж впасти.
#
# Використання:
#   bash scripts/restore_db.sh                 # db/backup/invest_agent_data.sql.gz
#   bash scripts/restore_db.sh /шлях/файл.gz   # власний файл
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO_ROOT="$PWD"

DUMP="${1:-$REPO_ROOT/db/backup/invest_agent_data.sql.gz}"

# DB_USER/DB_NAME із .env читаємо ГРЕПОМ, а не `source .env`: .env не
# є валідним shell-скриптом (напр. SEC_EDGAR_USER_AGENT містить пробіли
# — `source` спробував би виконати другу половину рядка як команду,
# живо впало 2026-10-13). Ці дві змінні — завжди одне слово.
_envget() {
  [ -f ".env" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" ".env" \
    | tail -1 | tr -d "\"'" | tr -d '\r'
}
DB_USER="${DB_USER:-$(_envget DB_USER)}"; DB_USER="${DB_USER:-invest_agent}"
DB_NAME="${DB_NAME:-$(_envget DB_NAME)}"; DB_NAME="${DB_NAME:-invest_agent}"

[ -f "$DUMP" ] || { echo "❌ Немає файлу резерву: $DUMP"; exit 1; }

echo "→ Відновлення $DB_NAME із $DUMP"
echo

# ─── 1. Контейнери підняті? ────────────────────────────────────────
if ! docker compose ps --status running --services 2>/dev/null | grep -qx db; then
  echo "❌ Сервіс db не запущено. Спершу: docker compose up -d --build"
  exit 1
fi
until docker compose exec -T db pg_isready -U "$DB_USER" -q 2>/dev/null; do
  echo "  · чекаю на готовність БД…"; sleep 2
done

# ─── 2. База вже наповнена? ────────────────────────────────────────
# Перевіряємо до застосування схеми: apply_schema.py створив би
# порожні таблиці, і далі ця перевірка вже нічого б не ловила.
# to_regclass, а не "select count(*) from raw_observations": на
# порожній базі таблиці ще немає, і такий запит упав би на ПАРСИНГУ —
# жодне WHERE EXISTS від цього не рятує. А глушити помилку через
# `|| echo 0` не можна: так само заглушився б і збій підключення, і
# гейт пропустив би накладання резерву на наповнену базу.
TBL="$(docker compose exec -T db psql -U "$DB_USER" -d "$DB_NAME" -q -t -A --no-psqlrc \
  -v ON_ERROR_STOP=1 -c "select to_regclass('public.raw_observations') is not null")"
if [ "$TBL" = "t" ]; then
  EXISTING="$(docker compose exec -T db psql -U "$DB_USER" -d "$DB_NAME" -q -t -A --no-psqlrc \
    -v ON_ERROR_STOP=1 -c 'select count(*) from public.raw_observations')"
else
  EXISTING=0
fi
if [ "${EXISTING:-0}" -gt 0 ]; then
  echo "❌ У raw_observations уже $EXISTING рядків — база не порожня."
  echo
  echo "   Резерв накладається тільки на ЧИСТУ базу: він містить COPY, а"
  echo "   не upsert, і накладання на наповнену базу або впаде на"
  echo "   унікальних обмеженнях, або подвоїть історію спостережень."
  echo
  echo "   Почати начисто (ВИДАЛИТЬ усі поточні дані!):"
  echo "     docker compose down -v && docker compose up -d --build"
  echo "     bash scripts/restore_db.sh"
  exit 1
fi

# ─── 3. Схема ──────────────────────────────────────────────────────
echo "  · схема (db/schema.sql)"
docker compose exec -T app python data-ingestion/apply_schema.py

# ─── 4. Дані ───────────────────────────────────────────────────────
# session_replication_role = replica (заданий у самому дампі) вимикає
# FK-тригери, тож порядок таблиць у файлі не має значення. Працює, бо
# DB_USER у образі postgres/timescaledb — суперюзер.
echo "  · дані ($(du -h "$DUMP" | cut -f1) стисненого SQL)"
zcat "$DUMP" | docker compose exec -T db \
  psql -U "$DB_USER" -d "$DB_NAME" -q --no-psqlrc -v ON_ERROR_STOP=1

# ─── 5. Перевірка живими даними ────────────────────────────────────
# Не "скрипт не впав", а скільки рядків реально лягло і чи FK цілі:
# session_replication_role = replica вставляє дані БЕЗ перевірки
# посилань, тож зламані посилання тут не впали б самі.
echo
echo "✅ Відновлено. Що в базі:"
echo
docker compose exec -T db psql -U "$DB_USER" -d "$DB_NAME" --no-psqlrc -c "
  select 'raw_observations' as t, count(*) as rows,
         min(observed_at)::date as from_date, max(observed_at)::date as to_date
    from raw_observations
  union all select 'sources',            count(*), null, null from sources
  union all select 'watchlist_assets',   count(*), null, null from watchlist_assets
  union all select 'backtest_verdicts',  count(*), null, null from backtest_verdicts
  union all select 'screening_results',  count(*), null, null from screening_results
  union all select 'trading_list',       count(*), null, null from trading_list
  union all select 'news_consolidated',  count(*), null, null from news_consolidated
  union all select 'llm_call_log',       count(*), null, null from llm_call_log
"

echo "Цілісність посилань (мусить бути 0 у кожному рядку):"
docker compose exec -T db psql -U "$DB_USER" -d "$DB_NAME" --no-psqlrc -c "
  select 'raw_observations→sources' as fk,
         count(*) as broken
    from raw_observations o left join sources s on s.name=o.source
   where s.name is null
  union all
  select 'результати→llm_call_log',
         (select count(*) from news_synthesis x       left join llm_call_log l on l.id=x.llm_call_id where x.llm_call_id is not null and l.id is null)
       + (select count(*) from market_synthesis x     left join llm_call_log l on l.id=x.llm_call_id where x.llm_call_id is not null and l.id is null)
       + (select count(*) from candidate_assets x     left join llm_call_log l on l.id=x.llm_call_id where x.llm_call_id is not null and l.id is null)
       + (select count(*) from expectation_synthesis x left join llm_call_log l on l.id=x.llm_call_id where x.llm_call_id is not null and l.id is null)
       + (select count(*) from metric_forecasts x     left join llm_call_log l on l.id=x.llm_call_id where x.llm_call_id is not null and l.id is null)
       + (select count(*) from news_consolidated x    left join llm_call_log l on l.id=x.llm_call_id where x.llm_call_id is not null and l.id is null)
       + (select count(*) from calendar_outlook x     left join llm_call_log l on l.id=x.llm_call_id where x.llm_call_id is not null and l.id is null)
       + (select count(*) from fundamental_analysis x left join llm_call_log l on l.id=x.llm_call_id where x.llm_call_id is not null and l.id is null)
  union all
  select 'expectation_synthesis→comparisons',
         count(*) from expectation_synthesis e
         left join expectation_comparisons c on c.id=e.comparison_id
        where e.comparison_id is not null and c.id is null
"

echo "Наступний крок — DEPLOYMENT.md, розділ «Після відновлення»."
