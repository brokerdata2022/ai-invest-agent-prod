#!/usr/bin/env bash
#
# Портативний резерв ДАНИХ агента: db/backup/invest_agent_data.sql.gz
#
# Резерв НЕ містить схеми. Схема — db/schema.sql (єдине джерело істини,
# .gitignore дев-репозиторію фіксує це прямим текстом), застосовується
# через data-ingestion/apply_schema.py. Причина такого розділення: дамп
# цілої TimescaleDB-бази (pg_dump без --data-only) тягне внутрішній
# каталог _timescaledb_catalog і вимагає timescaledb_pre_restore()/
# post_restore() при відновленні — ламається при найменшій розбіжності
# версій розширення між машинами. Дані ж через COPY у hypertable
# укладаються самі, чанки створює TimescaleDB — жодної привʼязки до
# версії.
#
# Використання:
#   bash scripts/backup_db.sh                 # у db/backup/ (для коміту)
#   bash scripts/backup_db.sh /шлях/файл.gz   # у власний шлях
#
# COMPOSE_DIR — тека, з якої виконувати `docker compose` (там мусять
# бути docker-compose.yml і .env). За замовчуванням це корінь цього
# репозиторію: на сервері резерв знімається з тієї ж бази, яку агент і
# наповнює. Інша тека потрібна, коли резерв знімають із БАЗИ ІНШОГО
# розгортання — саме так зроблено початковий резерв у db/backup/: дані
# жили в дев-проєкті, а лягти мусили в цей репозиторій.
#
#   COMPOSE_DIR=~/dev/ai-invest-agent bash scripts/backup_db.sh
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO_ROOT="$PWD"

OUT="${1:-$REPO_ROOT/db/backup/invest_agent_data.sql.gz}"
mkdir -p "$(dirname "$OUT")"

COMPOSE_DIR="${COMPOSE_DIR:-$REPO_ROOT}"
[ -f "$COMPOSE_DIR/docker-compose.yml" ] || {
  echo "❌ Немає $COMPOSE_DIR/docker-compose.yml — задайте COMPOSE_DIR"; exit 1; }

# DB_USER/DB_NAME із .env читаємо ГРЕПОМ, а не `source .env`: .env не
# є валідним shell-скриптом (напр. SEC_EDGAR_USER_AGENT містить пробіли
# — `source` спробував би виконати другу половину рядка як команду,
# живо впало 2026-10-13). Ці дві змінні — завжди одне слово.
_envget() {
  [ -f "$COMPOSE_DIR/.env" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$COMPOSE_DIR/.env" \
    | tail -1 | tr -d "\"'" | tr -d '\r'
}
DB_USER="${DB_USER:-$(_envget DB_USER)}"; DB_USER="${DB_USER:-invest_agent}"
DB_NAME="${DB_NAME:-$(_envget DB_NAME)}"; DB_NAME="${DB_NAME:-invest_agent}"

# ─── Що входить у резерв ────────────────────────────────────────────
#
# Порядок у масиві неважливий: відновлення йде з
# session_replication_role = replica (FK-тригери вимкнені), див.
# scripts/restore_db.sh.
TABLES=(
  # Незамінне. Append-only назавжди (правило 6): саме на цій історії
  # тримається якість прогнозів — ~285 точок на показник, добрані
  # окремим етапом. Повторний збір із нуля НЕ відтворить її: FRED/ECB
  # віддають глибину, а Binance/Twelve Data — ні.
  sources
  # raw_observations тут НЕМА навмисно — вона hypertable, і pg_dump -t
  # по ній віддає ПОРОЖНЬОГО батька: усі рядки лежать у чанках
  # _timescaledb_internal._hyper_1_*. Живий збій 2026-10-13: резерв
  # вийшов на 1.8 МБ замість 13 МБ із "raw_observations 0" у звіті, і
  # сам скрипт при цьому не впав. Дампиться окремо через
  # COPY (SELECT * FROM raw_observations) — читання через hypertable
  # бачить усі чанки, а при відновленні TimescaleDB сам розкладає
  # рядки назад. Перелічувати чанки іменами не можна: їх 300+ і вони
  # інші на кожній машині.

  # Теж незамінне, хоч і мале: на цих вердиктах
  # analysis/forecasting/method_selector.py вирішує, який показник іде
  # на ШІ, а який — на naive. Порожня таблиця = агент тихо повертається
  # до naive на ВСІХ 19 показниках, бо вердикту немає. Один прогін
  # backtest_llm.py коштує 50 LLM-викликів на показник.
  backtest_verdicts

  # Налаштоване людиною через Telegram (/watchlist_add) — у коді цього
  # немає, відтворити автоматично нічим.
  watchlist_assets

  # Календар релізів і ланцюжок факт/очікування навколо нього.
  release_log
  expectation_comparisons
  expectation_synthesis

  # Результати аналізу. Агент перегенерує їх за розкладом, але з ними
  # нове розгортання одразу має що показати в Telegram, а не чекає
  # першого циклу (годину-добу залежно від джоби).
  calendar_outlook
  candidate_assets
  company_filings
  crypto_long_candidates
  crypto_screening_candidates
  filing_insights
  fundamental_analysis
  market_synthesis
  metric_forecasts
  news_consolidated
  news_synthesis
  policy_rate_synthesis
  policy_speeches
  screening_results
  trading_list

  # Відпечатки вже надісланих повідомлень. Без них нове розгортання
  # повторно розсилає в Telegram те, що вже було надіслано.
  notify_fingerprints

  # 2026-10-22. Сирі дані SEC понад звітність (правило 6): 8-K і угоди
  # інсайдерів Form 4 акцій списку обраних. SEC віддає лише свіжі
  # подання, тож історія старша за вікно збору не відтвориться.
  company_events
  insider_transactions
  # Чек-листи чинників, складені ШІ (з 2026-10-20) — без них кожен
  # актив без ручного чек-листа перескладався б викликом ШІ.
  asset_checklists
  # Що вже забрано за подією earnings — без цього перший прогін після
  # відновлення перевіряв би в SEC усі звіти за 60 днів заново.
  earnings_refresh_log
  # Підсумки прогонів списку торгів (і порожніх) — для звіту "сьогодні
  # нічого" і детектора output_health.
  trading_list_runs
)
#
# ─── Що НЕ входить і чому ──────────────────────────────────────────
#
#   raw_news       — за дизайном живе 48г (RETENTION_HOURS у
#                    data-ingestion/common/news_db.py, джоба
#                    prune_raw_news). Виняток із правила 6, бо стаття
#                    тижневої давності не покращує прогноз.
#   news_analysis  — raw_news_id має NOT NULL + ON DELETE CASCADE на
#                    raw_news, тобто ці рядки зникають разом із
#                    новинами. Без raw_news у резерві вони дали б
#                    зламаний FK, а з ним — все одно зникли б за 48г.
#   llm_call_log   — 12 МБ аудиту минулих викликів. Повністю не
#                    береться, АЛЕ рядки, на які посилаються
#                    результати вище, додаються окремо (нижче):
#                    правило 5 вимагає, щоб під кожним висновком
#                    лишався його промпт і відповідь.
#   api_rate_limit, scheduler_heartbeat, telegram_command_offset
#                  — раннтайм-стан конкретної машини. Перенесений
#                    telegram_command_offset змусив би бота або
#                    пропустити команди, або переграти старі.

PG="docker compose --project-directory $COMPOSE_DIR exec -T db"
$PG psql -U "$DB_USER" -d "$DB_NAME" --no-psqlrc -c 'select 1' >/dev/null

# Список колонок таблиці БЕРЕМО З ЖИВОЇ БАЗИ, не вписуємо вручну:
# ручний список тихо розходиться зі схемою при кожному ALTER TABLE, а
# COPY з неправильними іменами впав би вже при відновленні — тобто на
# чужій машині, а не тут. Порядок attnum — той самий, що й у COPY без
# списку колонок.
_cols() {
  $PG psql -U "$DB_USER" -d "$DB_NAME" -q -t -A --no-psqlrc -c \
    "select string_agg(quote_ident(attname), ', ' order by attnum)
       from pg_attribute
      where attrelid = '$1'::regclass and attnum > 0 and not attisdropped"
}

echo "→ Резерв даних: $DB_NAME ($COMPOSE_DIR) → $OUT"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 1. Заголовок + вимкнення FK-тригерів на час відновлення.
cat > "$TMP/00_header.sql" <<SQL
--
-- Резерв ДАНИХ ai-invest-agent (без схеми).
-- Створено: $(date -Is)
-- Джерело: $DB_NAME @ $(hostname)
--
-- Відновлювати ТІЛЬКИ через scripts/restore_db.sh — він застосовує
-- схему (db/schema.sql) перед цим файлом. Пряме \\i у порожню базу
-- впаде: таблиць ще немає.
--
SET session_replication_role = replica;

--
-- db/schema.sql ЗАСІВАЄ ці дві таблиці (sources: 36 рядків джерел;
-- watchlist_assets: стартовий watchlist) — тобто після apply_schema.py
-- вони вже НЕ порожні, і COPY у них падає на дублікаті ключа. Живий
-- збій 2026-10-13, зловлений тестовим відновленням:
--   ERROR: duplicate key value violates unique constraint "sources_pkey"
--
-- Чистимо сід, бо резерв тут АВТОРИТЕТНІШИЙ за схему: watchlist
-- користувач редагує через Telegram (/watchlist_add), і в базі він
-- інший, ніж у seed-рядках schema.sql (напр. заміна джерел
-- wti_crude/brent_crude/coffee, docs/decisions.md 2026-10-04).
-- ON CONFLICT DO NOTHING тут не підходить саме тому: він залишив би
-- сід і тихо викинув справжній watchlist.
--
-- DELETE, не TRUNCATE: sources — батько raw_observations/release_log/
-- raw_news, а TRUNCATE на таблицю з посиланнями вимагає CASCADE і не
-- зважає на session_replication_role. FK у DELETE перевіряються
-- тригерами, тож під replica проходять.
--
-- Безпечно, бо restore_db.sh накладає резерв ЛИШЕ на чисту базу
-- (гейт raw_observations = 0) — видаляється тільки сід, не дані.
--
DELETE FROM public.watchlist_assets;
DELETE FROM public.sources;
SQL

# 2. Рядки llm_call_log, на які посилаються включені результати.
#    Першим після заголовка, щоб файл лишався читабельним і при
#    відновленні з увімкненими FK (на випадок не-суперюзера).
LLM_COLS="$(_cols llm_call_log)"
echo "  · llm_call_log (лише ті, на які є посилання)"
# COPY ... TO STDOUT віддає ЛИШЕ рядки даних (один рядок таблиці = один
# рядок файлу, переноси всередині значень екрануються як \n) — обгортку
# "COPY ... FROM stdin" і термінатор "\." додаємо самі, щоб файл
# виконувався як звичайний SQL.
{
  echo '--'
  echo '-- Data for Name: llm_call_log (partial); Type: TABLE DATA; Schema: public'
  echo '--'
  echo ''
  echo "COPY public.llm_call_log ($LLM_COLS) FROM stdin;"
  $PG psql -U "$DB_USER" -d "$DB_NAME" -q --no-psqlrc -v ON_ERROR_STOP=1 -c "
    COPY (
      with refd as (
        select llm_call_id from news_synthesis        where llm_call_id is not null
        union select llm_call_id from market_synthesis      where llm_call_id is not null
        union select llm_call_id from candidate_assets      where llm_call_id is not null
        union select llm_call_id from expectation_synthesis where llm_call_id is not null
        union select llm_call_id from metric_forecasts      where llm_call_id is not null
        union select llm_call_id from news_consolidated     where llm_call_id is not null
        union select llm_call_id from calendar_outlook      where llm_call_id is not null
        union select llm_call_id from fundamental_analysis  where llm_call_id is not null
      )
      select $LLM_COLS from llm_call_log l join refd on refd.llm_call_id = l.id
      order by l.id
    ) TO STDOUT"
  echo '\.'
  echo ''
  # Сіквенс ведемо від МАКСИМУМУ живої таблиці, не від максимуму
  # вибірки: інакше нові виклики на новій машині отримали б id, що вже
  # зустрічались у відкинутих рядках, і аудит-лог склеївся б із двох
  # різних історій під одним номером.
  echo "SELECT pg_catalog.setval('public.llm_call_log_id_seq', $(
    $PG psql -U "$DB_USER" -d "$DB_NAME" -q -t -A --no-psqlrc -c 'select coalesce(max(id),1) from llm_call_log'
  ), true);"
  echo ''
} > "$TMP/11_llm_call_log.sql"

# 3. raw_observations — через hypertable, не через pg_dump (див. вище).
OBS_COLS="$(_cols raw_observations)"
echo "  · raw_observations (hypertable, COPY через батька)"
{
  echo '--'
  echo '-- Data for Name: raw_observations; Type: TABLE DATA; Schema: public'
  echo '-- Hypertable: читається через батька, чанки створює TimescaleDB.'
  echo '--'
  echo ''
  echo "COPY public.raw_observations ($OBS_COLS) FROM stdin;"
  $PG psql -U "$DB_USER" -d "$DB_NAME" -q --no-psqlrc -v ON_ERROR_STOP=1 -c "
    COPY (
      select $OBS_COLS from raw_observations
       order by observed_at, source, metric_id, revision
    ) TO STDOUT"
  echo '\.'
  echo ''
  echo "SELECT pg_catalog.setval('public.raw_observations_id_seq', $(
    $PG psql -U "$DB_USER" -d "$DB_NAME" -q -t -A --no-psqlrc -c 'select coalesce(max(id),1) from raw_observations'
  ), true);"
  echo ''
} > "$TMP/15_raw_observations.sql"

# 3. Решта таблиць — pg_dump сам додає setval для їхніх сіквенсів.
echo "  · ${#TABLES[@]} таблиць через pg_dump --data-only"
PGARGS=()
for t in "${TABLES[@]}"; do PGARGS+=(-t "public.$t"); done
$PG pg_dump -U "$DB_USER" -d "$DB_NAME" \
  --data-only --no-owner --no-privileges --no-comments \
  "${PGARGS[@]}" > "$TMP/20_tables.sql"

# 4. Повернути FK-тригери.
printf '\nSET session_replication_role = DEFAULT;\n' > "$TMP/90_footer.sql"

cat "$TMP/00_header.sql" "$TMP/11_llm_call_log.sql" \
    "$TMP/15_raw_observations.sql" "$TMP/20_tables.sql" "$TMP/90_footer.sql" | gzip -9 > "$OUT"

# ─── Звірка: скільки рядків лягло проти того, скільки є в базі ─────
#
# Не "скрипт не впав", а ЗВІРКА. Саме цей гейт зловив живий збій
# 2026-10-13: pg_dump -t по hypertable raw_observations віддав
# порожнього батька, у резерв лягло 0 рядків замість 340 765, і без
# звірки це виглядало як успішний резерв на 1.8 МБ.
echo
echo "✅ $OUT ($(du -h "$OUT" | cut -f1))"
echo
printf '  %-32s %10s %10s\n' "ТАБЛИЦЯ" "У БАЗІ" "У РЕЗЕРВІ"

# Рядки COPY-блоків із самого резерву.
zcat "$OUT" | awk '
  /^COPY public\./ { t=$2; sub(/^public\./,"",t); n=0; inside=1; next }
  inside && /^\\\.$/ { print t"\t"n; inside=0; next }
  inside { n++ }
' | sort > "$TMP/in_backup.tsv"

# Рядки тих самих таблиць із живої бази.
ALL=("llm_call_log" "raw_observations" "${TABLES[@]}")
{
  for t in "${ALL[@]}"; do
    printf '%s\t%s\n' "$t" "$($PG psql -U "$DB_USER" -d "$DB_NAME" -q -t -A --no-psqlrc \
      -c "select count(*) from public.$t")"
  done
} | sort > "$TMP/in_db.tsv"

# llm_call_log у резерві навмисно вужчий за живий (лише рядки, на які
# посилаються включені результати) — для нього звіряємо не рівність, а
# "не порожньо". Решта мусить збігатись точно.
FAIL=0
while IFS=$'\t' read -r t db_n; do
  bk_n="$(awk -F'\t' -v k="$t" '$1==k{print $2}' "$TMP/in_backup.tsv")"
  bk_n="${bk_n:-0}"
  mark="  "
  if [ "$t" = "llm_call_log" ]; then
    [ "$db_n" -eq 0 ] || [ "$bk_n" -gt 0 ] || { mark="❌"; FAIL=1; }
  elif [ "$bk_n" != "$db_n" ]; then
    mark="❌"; FAIL=1
  fi
  printf '%s%-32s %10s %10s\n' "$mark" "$t" "$db_n" "$bk_n"
done < "$TMP/in_db.tsv"

echo
if [ "$FAIL" -ne 0 ]; then
  echo "❌ Резерв НЕПОВНИЙ — рядки з ❌ вище не збіглися з базою."
  echo "   Файл лишено для розбору, але розгортати з нього НЕ можна."
  exit 1
fi

echo "Схема в резерв НЕ входить (db/schema.sql). Відновлення:"
echo "  bash scripts/restore_db.sh"
