#!/usr/bin/env bash
# Крок 1 з 2 повного розгортання з нуля (README.md "Перший запуск з
# чистими даними"). Піднімає застосунок, застосовує схему, і запускає
# три одноразові масові backfill'и паралельно, без яких скринінг/синтез
# не мають даних, хоча код і тести коректні (docs/decisions.md,
# 2026-09-27 — жива перевірка "з нуля" показала саме цю прогалину).
#
# Скрипт ЧЕКАЄ на реальне завершення всіх трьох (`wait` на PID-и, не
# `docker compose exec -d` + негайний вихід) і сам друкує остаточний
# результат (успіх/провал кожного) — без цього не було надійного
# способу дізнатись, що дані справді зібрані, крім ручного вгадування
# по логах (фідбек користувача, 2026-09-27).
#
# Twelve Data (collect_universe.py) — ~500 тикерів S&P 500, ліміт
# 8 запитів/хв ⇒ ~65 хв. Це НАЙДОВША частина — решта значно швидша.
# Скрипт займає термінал на весь цей час; щоб не тримати сесію
# відкритою, запустіть його у фоні хоста самостійно, напр.:
#   nohup bash scripts/bootstrap_1_backfill.sh > logs/bootstrap_run.log 2>&1 &
#
# Використання:
#   bash scripts/bootstrap_1_backfill.sh
# Коли побачите "Усі три backfill'и завершились успішно" — запускайте
# scripts/bootstrap_2_pipeline.sh.

set -e

echo "== 1/4: піднімаємо контейнери =="
docker compose up -d --build

echo "== 2/4: застосовуємо схему БД =="
docker compose exec app python data-ingestion/apply_schema.py

echo "== 3/4: тести коду (НЕ перевіряють наявність даних, лише код) =="
docker compose exec app pytest -q

echo "== 4/4: запускаємо три одноразові backfill'и паралельно, чекаємо реального завершення =="
mkdir -p logs

docker compose exec -T app python data-ingestion/collect_universe.py > logs/bootstrap_quotes_universe.log 2>&1 &
pid_quotes=$!
docker compose exec -T app python data-ingestion/collect_companies_universe.py > logs/bootstrap_companies_universe.log 2>&1 &
pid_companies=$!
docker compose exec -T app python data-ingestion/collect_all.py --limit 15 > logs/bootstrap_collect_all.log 2>&1 &
pid_collect_all=$!

echo "Очікування (~1 год, здебільшого через ліміт Twelve Data 8 запитів/хв)..."
echo "PID-и (хост): quotes=$pid_quotes companies=$pid_companies collect_all=$pid_collect_all"

fail=0

if wait "$pid_quotes"; then
    echo "✅ collect_universe.py (ціни S&P 500) — завершено успішно"
else
    echo "❌ collect_universe.py (ціни S&P 500) — ПРОВАЛИВСЯ, дивись logs/bootstrap_quotes_universe.log"
    fail=1
fi

if wait "$pid_companies"; then
    echo "✅ collect_companies_universe.py (фундаментал SEC EDGAR) — завершено успішно"
else
    echo "❌ collect_companies_universe.py (фундаментал SEC EDGAR) — ПРОВАЛИВСЯ, дивись logs/bootstrap_companies_universe.log"
    fail=1
fi

if wait "$pid_collect_all"; then
    echo "✅ collect_all.py (глибша історія макропоказників) — завершено успішно"
else
    echo "❌ collect_all.py (глибша історія макропоказників) — ПРОВАЛИВСЯ, дивись logs/bootstrap_collect_all.log"
    fail=1
fi

echo
if [ "$fail" -eq 0 ]; then
    cat <<'EOF'
Усі три backfill'и завершились успішно.
Далі запустіть: bash scripts/bootstrap_2_pipeline.sh
EOF
else
    cat <<'EOF'
Один чи більше backfill'ів провалились (дивись повідомлення вище й
відповідні logs/bootstrap_*.log). Виправте причину і перезапустіть
цей скрипт — дедуп по source+external_id/ticker робить повторний
запуск безпечним, нічого не задублюється.
EOF
    exit 1
fi
