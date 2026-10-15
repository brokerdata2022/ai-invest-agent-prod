"""
Людські описи кожної джоби (JOBS/SCHEDULE) — для Telegram-команд
(docs/decisions.md 2026-10-03, рішення користувача: "команди мають
бути зрозумілі для користувача + назва джоби та опис що саме вона
робить"). Один рядок на джобу:
- `register_telegram_commands.py` реєструє їх через Telegram
  `setMyCommands` — назва+опис видно одразу в "/"-меню бота.
- `telegram_commands.py` показує той самий текст на /help.

tests/test_command_descriptions.py перевіряє, що жодна джоба з
schedule.py:SCHEDULE/jobs.py:JOBS не лишилась без опису (і навпаки).
Ліміт Telegram на описи команд — 1-256 символів, без переносу рядка.
"""

JOB_DESCRIPTIONS: dict[str, str] = {
    "refresh_calendar": "Оновити календар майбутніх релізів (на 3 тижні наперед)",
    "calendar_outlook": "Згенерувати ранковий огляд календаря (тиждень/день)",
    "notify_calendar_outlook": "Надіслати огляд календаря релізів",
    "news_collect_watchlist": "Зібрати новини по watchlist-активах (GDELT)",
    "revalidate_news_terms": "Перепровірити новинні терміни watchlist, що не пройшли перевірку",
    "news_collect_rss": "Зібрати геополітичні новини (RSS Fed/ECB/BOJ + редакційні)",
    "news_collect_stock": "Зібрати новини по тикерах зі скринінгу (GDELT)",
    "news_analysis": "DeepSeek-аналіз новин (усі потоки)",
    "news_consolidate": "Обʼєднати дублі та перекласти новини (усі потоки)",
    "news_merge_similar": "Об'єднати семантичні дублі новин між прогонами",
    "news_notify": "Надіслати найважливіші новини",
    "news_synthesis": "LLM-синтез причинного зв'язку ціна↔новини",
    "notify_synthesis": "Надіслати синтез ціна↔новини",
    "market_synthesis_asia": "Стан ринку після відкриття азіатської сесії",
    "market_synthesis_europe": "Стан ринку після відкриття європейської сесії",
    "market_synthesis_us": "Стан ринку після відкриття американської сесії",
    "notify_market_synthesis": "Надіслати стан ринку",
    "policy_rate_synthesis": "Прогноз рішення по ставці (5 входів → hold/cut/hike)",
    "notify_policy_rate": "Надіслати прогноз рішення по ставці",
    "filing_insights": "Витягти тези зі звітів 10-K/10-Q (текст, не цифри)",
    "fundamental_analysis": "Розбір активів за їхніми чинниками + звітність топ-акцій",
    "notify_fundamental": "Надіслати розгорнутий аналіз активів",
    "discover_candidates": "Пошук новачків: будь-який актив із рухом вгору або вниз",
    "notify_candidates": "Надіслати нових кандидатів-новачків",
    "daily_digest": "Щоденний дайджест найважливішого за добу",
    "crypto_prices": "Зібрати ціни BTC/ETH/SOL (Binance+CoinGecko)",
    "crypto_derivatives_collect": "Зібрати дані ф'ючерсів (Binance+Bybit+OKX)",
    "crypto_screening_daily": "Крипто-скринінг LONG/SHORT/WATCH (денний скан)",
    "crypto_candidates_monitor": "Погодинний моніторинг активних крипто-кандидатів",
    "watchlist_prices": "Зібрати ціни форекс/товарів watchlist (FRED/Twelve Data)",
    "quotes_universe_refresh": "Оновити ціни всього S&P 500 (Twelve Data)",
    "screening_composite_score": "Скринінг акцій S&P 500 (Tier A→B→C + ранжування)",
    "notify_screening": "Надіслати скринінг S&P 500",
    "notify_crypto_screening": "Надіслати крипто-кандидатів SHORT/WATCH",
    "trading_list": "Скласти список активів для розгляду (фільтр за каталізатором)",
    "notify_trading_list": "Надіслати список активів для розгляду",
    "notify_crypto_long": "Надіслати крипто-кандидатів LONG",
    "notify_watchlist": "Надіслати поточні ціни watchlist-активів",
    "companies_universe_refresh": "Оновити фундаментал усього S&P 500 (SEC EDGAR)",
    "macro_daily_series": "Зібрати денні макро-серії (облігації, ставка ФРС, USD/JPY)",
    "forecast_daily_series": "Прогноз денних серій (облігації, ставка ФРС, USD/JPY)",
    "safety_net_collect_all": "Страховий щомісячний перезбір усіх показників",
    "macro_crosscheck": "Звірити макродані між FRED і BLS (першоджерело)",
    "prune_logs": "Прибрати старі файли логів (понад 14 днів)",
    "prune_raw_news": "Прибрати старі новини з БД (понад 48г)",
    "prune_derived": "Прибрати старі ПОХІДНІ результати (сирі дані не чіпає)",
    "evaluate_outcomes": "Оцінити постфактум: що сталось із рішеннями агента",
    "notify_outcomes": "Надіслати відсоток влучань рішень за горизонт",
    "output_health": "Перевірити, чи виводи агента не виродились",
    "earnings_calendar": "Зібрати календар звітності компаній (Nasdaq)",
    "quotes_crosscheck": "Звірити котирування акцій і крипти з другим джерелом",
    "scheduler_heartbeat": "Записати пульс планувальника (діагностика простою)",
    "release_pipeline": "Релізний конвеєр: детекція → прогноз → сюрприз → звіти",
    "telegram_commands": "Опитати Telegram і виконати нові команди",
}

# Команди редагування watchlist (2026-10-03, docs/decisions.md) — НЕ
# джоби з JOBS (своя гілка в telegram_commands.py:process_update()),
# але так само потребують назви+опису в "/"-меню Telegram і на /help.
SPECIAL_COMMAND_DESCRIPTIONS: dict[str, str] = {
    "ask": "Запитати агента про його дані (можна й просто текстом, без команди)",
    "watchlist_list": "Показати поточний watchlist",
    "watchlist_add": "Додати актив у watchlist (пробує Twelve Data, потім Binance)",
    "watchlist_remove": "Вимкнути актив з watchlist",
}

# Підмножина JOB_DESCRIPTIONS, видима й виконувана через Telegram
# (2026-10-03, рішення користувача: "забери всі технічні джоби з меню,
# лиши тільки для отримання інформації"). Решта 32 джоб (збір даних,
# промiжні кроки конвеєра, обслуговування) і далі працюють за
# розкладом (orchestration/schedule.py) без змін — просто більше не
# запускаються вручну з Telegram; ручний запуск лишається через
# `docker compose exec app python orchestration/run_job.py <назва>`.
# Критерій — джоба сама НАДСИЛАЄ користувачу інформацію (notify_*/
# daily_digest), не промiжний крок конвеєра (збір/аналіз/скринінг без
# доставки).
INFO_JOB_NAMES: frozenset[str] = frozenset({
    "notify_calendar_outlook",
    # Три окремі notify-команди (`notify_expectations`,
    # `notify_forecasts`, `notify_release_impact`) злиті в
    # `release_pipeline` 2026-10-13 — вони більше не існують як окремі
    # джоби. Ручний запуск конвеєра дає все три звіти одразу, і це
    # навіть зручніше: вони завжди потрібні разом (сюрприз без
    # прогнозу поруч читається гірше).
    "release_pipeline",
    "notify_policy_rate",
    "news_notify",
    "notify_synthesis",
    "notify_market_synthesis",
    "notify_candidates",
    "daily_digest",
    "notify_screening",
    "notify_crypto_screening",
    "notify_crypto_long",
    "notify_trading_list",
    "notify_fundamental",
    "notify_watchlist",
})
