#!/usr/bin/env bash
# Крок 2 з 2 повного розгортання з нуля — запускайте ЛИШЕ ПІСЛЯ того,
# як усі три backfill'и з scripts/bootstrap_1_backfill.sh завершились
# (без них screening/synthesize не матимуть даних).
#
# Прогонить увесь конвеєр ОДИН РАЗ, у порядку залежностей, через
# orchestration/run_job.py — той самий код, що виконує планувальник
# автоматично (docker-compose сервіс scheduler), тож це не "інший
# шлях для ручного тесту", а справжня перевірка продакшн-автоматизації.
#
# Best-effort, як collect_all.py: одна джоба, що впала (напр. GDELT
# 429 — задокументований, транзієнтний ризик спільного dev-IP,
# docs/decisions.md), НЕ повинна зупиняти решту незалежних кроків
# (ціни/SEC/RSS/DeepSeek/синтез/дайджест) — живо виявлено 2026-09-27:
# попередня версія мала `set -e`, і провал news_collect_watchlist
# обірвав увесь конвеєр ще до screening-кроків, які від нього не
# залежать. Підсумок по всіх джобах — наприкінці.
#
# Використання:
#   bash scripts/bootstrap_2_pipeline.sh

ok=()
failed=()

run() {
    echo "== $1 =="
    if docker compose exec app python orchestration/run_job.py "$1"; then
        ok+=("$1")
    else
        failed+=("$1")
    fi
}

echo "--- Календар релізів (щоб не чекати до понеділка — жива діра, знайдена 2026-09-27: без цього release_log лишається порожнім аж до першого запланованого refresh_calendar, і check_releases/compare_expectations/synthesize_expectations/notify_expectations просто нічого не роблять на свіжому розгортанні) ---"
run refresh_calendar
run check_releases
run compare_expectations
run synthesize_expectations
run notify_expectations

echo "--- Скринінг (Tier A/B/C, потребує свіжий universe з кроку 1) ---"
run screening_composite_score

echo "--- Збір новин (усі потоки) ---"
run news_collect_watchlist
run news_collect_stock
run news_collect_general
run news_collect_rss

echo "--- Ціни (watchlist-товари/форекс/золото) ---"
run watchlist_prices
# quotes_universe_refresh (весь S&P 500 universe, Twelve Data) СВІДОМО
# пропущено тут — bootstrap_1_backfill.sh щойно зробив рівно те саме
# (collect_universe.py, той самий код) хвилини тому; другий прогін
# ~65-80 хв через ліміт 8 запитів/хв дав би нуль нових даних, а нижче
# по конвеєру ніщо на нього не чекає (screening уже відпрацював на
# наявних цінах). Живо виявлено 2026-09-27 (bootstrap "з нуля" одразу
# після backfill'у — саме той сценарій, для якого написаний README).
# У продакшені джоба й так іде щодня о 3:00 (orchestration/schedule.py)
# — тут вона просто не потрібна ще раз.

echo "--- DeepSeek-класифікація зібраних новин ---"
run news_analysis_watchlist
run news_analysis_general
run news_analysis_geopolitical

echo "--- LLM-синтез (Цілі 1/2/3/4, docs/decisions.md) ---"
run news_synthesis
run market_synthesis
run discover_candidates

echo "--- Щоденний дайджест (Ціль 5) ---"
run daily_digest

echo
echo "--- Підсумок ---"
echo "Успішно (${#ok[@]}): ${ok[*]:-(нічого)}"
if [ "${#failed[@]}" -gt 0 ]; then
    echo "Провалилось (${#failed[@]}): ${failed[*]}"
    echo "Лог кожної — logs/<назва>_*.log. Транзієнтний збій (напр. GDELT 429)"
    echo "безпечно перезапустити окремо:"
    echo "  docker compose exec app python orchestration/run_job.py <назва>"
    exit 1
fi

cat <<'EOF'

Готово. Увесь конвеєр прогнано один раз вручну й підтверджено робочим.
Далі сервіс scheduler (docker compose ps) продовжує сам за розкладом
orchestration/schedule.py — жодних ручних кроків більше не потрібно.
EOF
