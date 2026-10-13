#!/usr/bin/env python3
"""
Ручний запуск збору новин (GDELT) для потоку **watchlist** —
цільовий запит по конкретних активах (docs/watchlist.md, не-акційна
частина). Акції зі скринінгу — окремий скрипт,
analysis/news_analysis/collect_stock_news.py.

**Через GDELT тут лишився ЄДИНИЙ потік.** Два інші прибрані з тієї
самої причини, але в різний час:
- `geopolitical` (2026-09-27) — курований GDELT-запит губив
  непередбачені події, замінено на 9 широких RSS-фідів;
- `general` (2026-10-06) — RSS-фіди ринкових видань дають ~140 статей
  на добу, тоді як GDELT на тому самому IP у тривалому штрафному
  стані (429 + мʼякий троттл) і джоба падала щопрогону. Живий фідбек
  користувача: "опять провалилися джоби... чому не застосовано
  резервне джерело автоматично?" — резерв працював на рівні ДАНИХ
  (RSS), але джоба GDELT про нього не знала й далі сигналізувала
  провал, хоч потік був живий. Прибрати джерело, яке нічого не додає
  і щоразу падає, честніше, ніж гасити його сигнал.

**Чому watchlist лишається саме на GDELT і БЕЗ резерву:** тільки
пошуковий запит умієте "лише про золото". RSS такого не вміє — у
нього є лише те, що редакція поклала у стрічку. Тому тут збій
ЗАЛИШАЄТЬСЯ гучним (exit 1): це єдине джерело, і його втрата —
справжня втрата даних, не шум. Частковий замінник усе ж є: статті
потоку `general` з RSS тепер теж тегуються watchlist-активами
(run_news_analysis.py), тож новини про золото/нафту/BTC доходять до
синтезу й без цільового запиту.

Використання:
    python run_collect_news.py --stream watchlist
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.db import get_connection  # noqa: E402
from common.news_db import insert_news_batch  # noqa: E402
from common.rate_limit import make_pacer  # noqa: E402
from common.watchlist_db import fetch_terms  # noqa: E402
from news.gdelt_adapter import GdeltAdapter, GdeltError  # noqa: E402
from news.queries import build_watchlist_query  # noqa: E402

from config import (  # noqa: E402
    GDELT_MIN_REQUEST_INTERVAL_SECONDS,
    GDELT_THROTTLE_MAX_WAIT_SECONDS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _build_watchlist_queries() -> list[str]:
    """Власне, коротке з'єднання лише для читання живих watchlist-
    термінів (common/watchlist_db.py:fetch_terms, редагується через
    Telegram, docs/decisions.md 2026-10-03) — окремо від головного
    conn нижче (insert_news_batch), щоб не змінювати структуру main()
    заради одного раннього читання."""
    conn = get_connection()
    try:
        terms = fetch_terms(conn)
    finally:
        conn.close()
    return [build_watchlist_query(terms)]


# Білдер повертає list[str] — кілька окремих GDELT-запитів для того
# самого стріму (watchlist — один, обгорнутий у список для однакової
# обробки нижче). `general` прибраний 2026-10-06 (див. докстрінг
# модуля), його запити лишаються в news/queries.py невикористаними —
# не видаляю разом із джобою, щоб повернення не вимагало писати їх
# заново, якщо GDELT колись стане надійним.
QUERY_BUILDERS = {
    "watchlist": _build_watchlist_queries,
}


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", choices=sorted(QUERY_BUILDERS), default="watchlist")
    parser.add_argument("--maxrecords", type=int, default=75)
    parser.add_argument(
        "--timespan", default="1d",
        help="скільки часу назад шукати (GDELT-формат, напр. 1d/3d) — "
             "без обмеження GDELT віддає найновіші maxrecords збігів "
             "БЕЗ огляду на давність, це можуть бути місяці старі статті",
    )
    args = parser.parse_args()

    queries = QUERY_BUILDERS[args.stream]()

    # Зʼєднання відкривається ПЕРЕД збором (а не лише для запису
    # нижче), бо глобальний обмежувач швидкості GDELT живе в БД —
    # інакше три джоби збору новин знову перевищили б ліміт
    # 1 запит/5с (docs/decisions.md, 2026-10-05).
    conn = get_connection()
    pacer = make_pacer(
        conn,
        api="gdelt",
        min_interval_seconds=GDELT_MIN_REQUEST_INTERVAL_SECONDS,
        max_wait_seconds=GDELT_THROTTLE_MAX_WAIT_SECONDS,
    )

    all_records = []
    any_query_failed = False
    for query in queries:
        logger.info("GDELT query (%s, timespan=%s): %s", args.stream, args.timespan, query)
        adapter = GdeltAdapter(stream=args.stream, query=query, pacer=pacer)
        try:
            records = adapter.collect(maxrecords=args.maxrecords, timespan=args.timespan)
        except (requests.exceptions.RequestException, GdeltError):
            # Транзієнтний збій GDELT (429 понад retry-бюджет адаптера,
            # мережевий тайм-аут) — той самий підхід, що
            # collect_stock_news.py/run_collect_rss.py: лог без
            # трасування, ОДИН провалений query не повинен зупиняти
            # решту (general тепер два незалежні запити).
            logger.exception("Пропущено query (мережева помилка GDELT): %s", query)
            any_query_failed = True
            continue
        logger.info("Отримано %d статей", len(records))
        all_records.extend(records)

    if not all_records:
        conn.close()
        if any_query_failed:
            # Усі запити впали (network/429). ДО 2026-10-12 тут був
            # exit(1) з обґрунтуванням "у цього потоку РЕЗЕРВУ НЕМА",
            # і це було правдою. Тепер неправда: резерв є —
            # детермінований пошук watchlist-термінів у вже зібраному
            # RSS-корпусі (`config.WATCHLIST_MATCH_TERMS` +
            # `run_news_analysis.py:matched_watchlist_assets`), тому
            # недоступність GDELT більше не означає втрату потоку.
            #
            # Чому саме exit(0), а не "залишити алерт про всяк
            # випадок": 12 жовтня GDELT упав ВСІ 4 прогони, і
            # користувач отримав 4 трасування стека в Telegram за
            # добу — сигналізація про стан, який ми вже вміємо
            # пережити, це рівно той шум, через який справжні
            # повідомлення губляться. Факт недоступності лишається в
            # лозі, не в чаті.
            #
            # ⚠️ ПЕРЕВІРИТИ НА ПРОДАКШЕНІ (новий IP, VPS): бан GDELT —
            # ЗА IP, не за ключем (ключа в нього немає). На свіжій
            # адресі запит може знову працювати. Якщо так — GDELT
            # повертається як ДОДАТКОВЕ джерело обсягу (75 статей на
            # запит проти ~10 у пошуку по корпусу), але вже НЕ як
            # єдина опора потоку. Перевірка: прогнати цю джобу вручну
            # й подивитись "Отримано N статей" у лозі.
            logger.error(
                "Усі GDELT-запити (%s) провалились — працюємо на резерві: "
                "пошук watchlist-термінів у вже зібраному RSS-корпусі "
                "(config.WATCHLIST_MATCH_TERMS). Обсяг менший за цільовий "
                "запит GDELT; на новому IP перевірити, чи бан діє.",
                args.stream,
            )
            sys.exit(0)
        logger.warning("Немає статей — перевірте query(і) чи доступність GDELT")
        sys.exit(0)

    try:
        inserted = insert_news_batch(conn, all_records)
    finally:
        conn.close()

    logger.info("Готово: %d нових статей записано в raw_news", inserted)


if __name__ == "__main__":
    main()
