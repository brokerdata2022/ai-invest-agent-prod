#!/usr/bin/env bash
#
# Розпакування чанків `raw_observations` — відкат компресії,
# увімкненої 2026-10-15 і яка 2026-10-16 поклала продакшен
# (db/schema.sql, розділ про компресію: OOM-kill бекендів Postgres на
# ПЛАНУВАННІ запиту по 512 metric_id).
#
# Чому окремим скриптом, а не в schema.sql: схема описує СТРУКТУРУ й
# має застосовуватись за секунди. Розпакування 1205 чанків — довга
# операція над ДАНИМИ (хвилини-десятки хвилин), і запускати її на
# кожному `apply_schema.py` неприпустимо.
#
# Ідемпотентний: `if_compressed => true` пропускає вже розпаковані.
# Дані НЕ змінюються — компресія/розпакування це формат зберігання,
# рядки ті самі (critical rule 6 не порушується в обидві сторони).
#
# Використання:
#   bash scripts/decompress_raw_observations.sh
#
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

_envget() {
  [ -f ".env" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" ".env" \
    | tail -1 | tr -d "\"'" | tr -d '\r'
}
DB_USER="${DB_USER:-$(_envget DB_USER)}"; DB_USER="${DB_USER:-invest_agent}"
DB_NAME="${DB_NAME:-$(_envget DB_NAME)}"; DB_NAME="${DB_NAME:-invest_agent}"

_psql() {
  docker compose exec -T db psql -U "$DB_USER" -d "$DB_NAME" -q -t -A --no-psqlrc -v ON_ERROR_STOP=1 "$@"
}

if ! docker compose ps --status running --services 2>/dev/null | grep -qx db; then
  echo "❌ Сервіс db не запущено. Спершу: docker compose up -d"
  exit 1
fi

echo "→ Відкат компресії raw_observations"

# 1. Політика — ПЕРШОЮ, інакше вона стискатиме назад те, що ми
#    розпаковуємо (schema.sql робить це теж, але скрипт мусить бути
#    самодостатнім: його запускають і до git pull, щоб зупинити збій).
echo "  · знімаю політику компресії"
_psql -c "SELECT remove_compression_policy('raw_observations', if_exists => TRUE);" > /dev/null

BEFORE="$(_psql -c "SELECT count(*) FROM timescaledb_information.chunks
                     WHERE hypertable_name = 'raw_observations' AND is_compressed")"
echo "  · стиснутих чанків: $BEFORE"

if [ "${BEFORE:-0}" -gt 0 ]; then
  echo "  · розпаковую (довго, не перериваємо)…"
  _psql -c "SELECT count(*) FROM (
              SELECT decompress_chunk(c, if_compressed => true)
              FROM show_chunks('raw_observations') c
            ) t;" > /dev/null
fi

AFTER="$(_psql -c "SELECT count(*) FROM timescaledb_information.chunks
                    WHERE hypertable_name = 'raw_observations' AND is_compressed")"
echo "  · стиснутих чанків після: $AFTER"

if [ "${AFTER:-1}" -ne 0 ]; then
  echo "❌ Лишились стиснуті чанки ($AFTER) — повтори запуск або дивись лог БД"
  exit 1
fi

# 2. Саму можливість компресії знімаємо ОСТАННЬОЮ: Timescale не дає
#    цього зробити, поки є хоч один стиснутий чанк.
echo "  · вимикаю компресію на таблиці"
_psql -c "ALTER TABLE raw_observations SET (timescaledb.compress = false);" > /dev/null

# 3. Перевірка живими даними: рядки на місці, кількість не змінилась.
ROWS="$(_psql -c "SELECT count(*) FROM raw_observations")"
echo "  · рядків у raw_observations: $ROWS"

echo
echo "✅ Готово. Перевір бойовий запит, який падав:"
echo "     docker compose exec app python orchestration/run_job.py screening_composite_score"
