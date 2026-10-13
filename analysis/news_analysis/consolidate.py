#!/usr/bin/env python3
"""
Консолідація сирих новин у потоку: МЕХАНІЧНА кластеризація (без LLM)
+ ОДИН LLM-виклик на потік для того, що механіка не вміє (2026-09-28,
рішення користувача: "проблема лежить в оцінках, думай як виправити
саму оцінку, а не поріг").

## Чому не "просто один LLM на все", як було раніше

Перша версія (2026-09-28, попередній запис docs/decisions.md) давала
ВСІ сирі статті (до 60) в один промпт і просила LLM одночасно
відкинути шум, ОБ'ЄДНАТИ дублі й оцінити "важливість" — усе в одному
проході. На практиці LLM-`confidence` виявився ненадійним мірилом
важливості: одиничний "downgrade без цифр" від сумнівного сайту міг
отримати 0.9, а реальний BTC $84k — 0.6. Причина — жодної зовнішньої
точки опори, чиста суб'єктивна калібрація по 60 різнорідних темах
одразу.

## Новий підхід: механіка робить, що вміє добре; LLM — що вміє добре

1. **Кластеризація дублікатів — МЕХАНІЧНА** (`aggregate.py:cluster_articles()`,
   уже існує в проєкті для post-analysis, тепер працює й на сирих
   статтях — title similarity, без LLM). `source_count` кластера —
   ОБ'ЄКТИВНИЙ факт (скільки незалежних сайтів написали те саме), не
   LLM-здогадка. Це й є "механічний фільтр перед AI", про який просив
   користувач.
2. **LLM отримує ПРЕДСТАВНИКІВ кластерів** (не кожну сиру статтю
   окремо) — і робить тільки те, що вимагає розуміння тексту:
   фільтрує нерелевантне, домержує СЕМАНТИЧНІ дублікати (та сама подія
   різними словами/мовами, які title-схожість не спіймала), перекладає.
3. **Пріоритизація (reporting/news_notify.py) — за source_count
   (механіка), не за LLM-confidence.** `confidence` лишається лише
   фільтром "чи це взагалі релевантно" (ворота), не мірилом
   важливості.

Джерела (`source_urls`/`source_raw_news_ids`) приєднує КОД за
кластерами, які повернув DeepSeek — не сам DeepSeek (ризик
галюцинації URL, той самий принцип, що relevance_filter.py/synthesize.py).

Використання:
    python consolidate.py --stream watchlist
    python consolidate.py --stream general --max-age-days 1
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
import requests

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_INGESTION_DIR = os.path.join(_ANALYSIS_DIR, "..", "data-ingestion")
sys.path.insert(0, _ANALYSIS_DIR)
sys.path.insert(0, _DATA_INGESTION_DIR)

from common.db import get_connection  # noqa: E402
from llm_common import (  # noqa: E402
    LlmError,
    DIRECTIONS,
    call_llm,
    log_llm_call,
    parse_confidence,
    parse_json_object,
    require_api_key,
)
from common.watchlist_db import fetch_asset_ids  # noqa: E402
from news_analysis._consolidated_db import (  # noqa: E402
    fetch_recent_summaries,
    fetch_unconsolidated_raw_news,
    is_duplicate_of_recent,
    mark_consolidated,
    save_consolidated_item,
)
from common.news_adapter import STREAMS  # noqa: E402
from news_analysis.aggregate import NewsCluster, cluster_articles  # noqa: E402
from news_analysis.relevance_filter import extract_description  # noqa: E402
from screening._results_db import fetch_latest_tickers  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_REQUIRED_ITEM_FIELDS = ("summary", "direction", "confidence", "reasoning", "source_indices")

SYSTEM_PROMPT = (
    "Ти фінансовий новинний редактор. Тобі дають ПРОНУМЕРОВАНИЙ список "
    "ІСТОРІЙ (кожна вже може бути підтверджена кількома незалежними "
    "сайтами — вказано в дужках; ідентичні за заголовком статті вже "
    "об'єднані механічно, ДО тебе) — будь-якою мовою."
    "\n\n"
    "Твоя задача — ТРИ речі одним проходом:"
    "\n"
    "1. ВІДКИНУТИ нерелевантне — включай історію в items ТІЛЬКИ якщо "
    "вона дає РЕАЛЬНУ, конкретну інформацію хоча б одного типу: "
    "(а) економічний показник/тренд із конкретними цифрами чи "
    "напрямком (інфляція, ставки, ВВП, зайнятість, звітність компанії "
    "з цифрами), (б) геополітична подія З ЯВНО НАЗВАНИМ економічним "
    "наслідком у самій історії — санкції/тарифи з конкретними товарами "
    "чи сумами, перекриття торгового маршруту/протоки, рішення "
    "центробанку/уряду, що прямо міняє ставки/валюту/бюджет. НЕ "
    "ДОСИТЬ, щоб подія була 'геополітично значуща' сама по собі — "
    "історія повинна НАЗИВАТИ конкретний економічний наслідок (ціна "
    "нафти/валюта/торгівля), інакше це просто світові новини, не "
    "ринкові. (в) конкретний вплив на ціну/тренд активу (злиття, IPO "
    "з цифрами, зміна кредитного рейтингу, суттєвий рух ціни з "
    "причиною). ОКРЕМО: downgrade/upgrade рейтингу акції — це "
    "релевантна інформація (в) ТІЛЬКИ якщо названо конкретну цільову "
    "ціну/відсоток/причину; сама фраза 'знижено до продавати' БЕЗ "
    "жодної цифри — низька якість (типово SEO-агрегатори рейтингів), "
    "НЕ включай, незалежно від кількості джерел. ВИКЛЮЧИ ПОВНІСТЮ: "
    "місцеві новини без міжнародного економічного виміру, "
    "бізнес-персоналії без цифр, анонси звітів БЕЗ самих цифр, "
    "поради/колонки думок, спорт, кримінал, культура, PR-анонси, "
    "тероризм/військові дії/стихійні лиха/внутрішня політика БЕЗ явно "
    "названого економічного наслідку в тексті. Якщо сумніваєшся — "
    "виключи."
    "\n"
    "2. ДОМЕРЖИТИ (не обов'язково для більшості): якщо кілька "
    "ІСТОРІЙ у списку насправді про ту саму подію іншими словами чи "
    "мовою (механічне групування за заголовком уже об'єднало "
    "ідентичне, але не перефразоване) — познач їхні номери РАЗОМ в "
    "одному source_indices."
    "\n"
    "3. ПЕРЕКЛАСТИ підсумок кожного запису УКРАЇНСЬКОЮ, незалежно від "
    "мови оригіналу."
    "\n\n"
    "confidence тут — НАСКІЛЬКИ ТИ ВПЕВНЕНИЙ У РЕЛЕВАНТНОСТІ (чи "
    "справді ринкова новина), НЕ оцінка важливості/масштабу — "
    "важливість визначає кількість джерел (уже видно з дужок), не ти."
    "\n\n"
    "НІКОЛИ не давай прямих торгових рекомендацій ('купити'/'продати'/"
    "'входити в позицію') — тільки описові характеристики. Відповідай "
    "ЛИШЕ JSON-об'єктом з полем 'items' — списком об'єктів: "
    "asset_id (string з переліку відстежуваних активів, якщо новина "
    "прямо про нього, інакше null), summary (1-2 речення українською, "
    "тільки конкретні факти), direction (up/down/neutral/unclear), "
    "confidence (0..1 — релевантність, не важливість), reasoning "
    "(коротке обґрунтування для аудиту), source_indices (список "
    "номерів ІСТОРІЙ із вхідного списку, що описують ту саму подію — "
    "зазвичай один номер, кілька лише коли домержуєш за п.2)."
)


class ConsolidationResponseError(ValueError):
    pass


def _cluster_description(cluster: NewsCluster, articles_by_id: dict) -> str | None:
    """Опис ПРЕДСТАВНИКА кластера (той самий текст, який
    _build_cluster() обрав як representative_title) — якщо джерело
    його дало (RSS дає, GDELT ні)."""
    for raw_id in cluster.raw_news_ids:
        article = articles_by_id.get(raw_id)
        if article and article["title"] == cluster.representative_title:
            return extract_description(article)
    return None


def build_prompt(clusters: list[NewsCluster], articles_by_id: dict, tracked_assets: list[str] = None) -> str:
    lines = []
    if tracked_assets:
        lines.append(f"Відстежувані активи: {', '.join(tracked_assets)}")
        lines.append("")
    lines.append(f"Історії ({len(clusters)}):")
    for i, cluster in enumerate(clusters, start=1):
        lines.append(f"{i}. [{cluster.source_count} джерел] {cluster.representative_title}")
        lines.append(f"   Дата: {cluster.latest_published_at}")
        description = _cluster_description(cluster, articles_by_id)
        if description:
            lines.append(f"   Опис: {description}")
    return "\n".join(lines)


def parse_response(raw_content: str, n_clusters: int) -> list[dict]:
    data = parse_json_object(raw_content, ConsolidationResponseError)

    items = data.get("items")
    if items is None:
        raise ConsolidationResponseError(f"У відповіді LLM бракує поля 'items': {data!r}")
    if not isinstance(items, list):
        raise ConsolidationResponseError(f"'items' має бути списком: {items!r}")

    for item in items:
        missing = [f for f in _REQUIRED_ITEM_FIELDS if f not in item]
        if missing:
            raise ConsolidationResponseError(f"Запису бракує полів {missing}: {item!r}")

        if item["direction"] not in DIRECTIONS:
            raise ConsolidationResponseError(f"Неочікуване значення direction: {item['direction']!r}")
        item["confidence"] = parse_confidence(item["confidence"], ConsolidationResponseError)

        indices = item["source_indices"]
        if not isinstance(indices, list) or not indices:
            raise ConsolidationResponseError(f"source_indices має бути непорожнім списком: {indices!r}")
        for idx in indices:
            if not isinstance(idx, int) or not (1 <= idx <= n_clusters):
                raise ConsolidationResponseError(
                    f"source_indices містить недійсний індекс {idx!r} (маємо 1..{n_clusters})"
                )

    return items


def consolidate_stream(
    conn, stream: str, articles: list[dict], tracked_assets: list[str] = None
) -> list[dict]:
    """Механічна кластеризація → промпт із представників → LLM →
    парсить → зберігає кожен запис, що НЕ дублює вже збережене цього
    потоку за вікно (`is_duplicate_of_recent()` — той самий факт з
    ІНШОГО прогону консолідації, не спійманий ні кластеризацією, ні
    LLM-домерджуванням, бо обидва бачать лише статті ОДНОГО прогону).
    Повертає ФАКТИЧНО збережені записи (для логування в main()).
    tracked_assets=None лишається None (без фолбеку тут) — для
    stream="watchlist" ЄДИНЕ місце, що рахує живий список, це
    main() (fetch_asset_ids(conn) + fetch_latest_tickers(conn)) —
    один DB-виклик, не два різні шляхи того самого."""
    # Крок 1 — МЕХАНІЧНА кластеризація (без LLM). cluster_articles()
    # очікує raw_news_id (не id) — той самий формат, що post-analysis
    # виклик у aggregate.py, для сумісності обох сценаріїв.
    clustering_input = [{**a, "raw_news_id": a["id"]} for a in articles]
    clusters = cluster_articles(clustering_input)
    articles_by_id = {a["id"]: a for a in articles}

    prompt = build_prompt(clusters, articles_by_id, tracked_assets)
    llm = call_llm(prompt, SYSTEM_PROMPT, purpose="news_consolidation")
    raw_content = llm.text
    items = parse_response(raw_content, len(clusters))

    llm_call_id = log_llm_call(
        conn, llm=llm, purpose="news_consolidation",
        prompt=prompt, source_ref=stream,
    )

    recent_summaries = fetch_recent_summaries(conn, stream)

    saved = []
    for item in items:
        if is_duplicate_of_recent(item["summary"], recent_summaries):
            logger.info("%s: пропущено як дублікат вже збереженого — %s", stream, item["summary"])
            continue

        # Кластери, об'єднані ЦИМ записом (зазвичай один) — мехаchнічний
        # source_count = СУМА джерел усіх об'єднаних кластерів, не
        # LLM-здогадка (docstring модуля: пріоритизація за цим числом,
        # не за confidence).
        source_clusters = [clusters[i - 1] for i in item["source_indices"]]
        source_raw_news_ids = [rid for c in source_clusters for rid in c.raw_news_ids]
        source_urls = [url for c in source_clusters for url in c.urls]
        mechanical_source_count = sum(c.source_count for c in source_clusters)

        item_id = save_consolidated_item(
            conn,
            stream=stream,
            asset_id=item.get("asset_id") or None,
            summary=item["summary"],
            direction=item["direction"],
            confidence=item["confidence"],
            reasoning=item["reasoning"],
            source_raw_news_ids=source_raw_news_ids,
            source_urls=source_urls,
            llm_call_id=llm_call_id,
        )
        saved.append({"id": item_id, "source_count": mechanical_source_count, **item})
        recent_summaries.append(item["summary"])  # і в межах цього прогону теж
    return saved


def consolidate_one_stream(conn, stream: str, max_age_days: int, limit: int) -> int:
    """Консолідація ОДНОГО потоку. Повертає скільки історій збережено.

    Винесено з `main()` 2026-10-13, коли три джоби
    (`news_consolidate_watchlist`/`_general`/`_geopolitical`) злили в
    одну: цикл по потоках потребує, щоб збій на одному потоці НЕ
    зупиняв решту — раніше цю ізоляцію давав сам факт трьох окремих
    джоб, тепер її мусить давати код."""
    articles = fetch_unconsolidated_raw_news(
        conn, stream=stream, max_age_days=max_age_days, limit=limit
    )
    if not articles:
        logger.info("%s: немає нових сирих статей для консолідації", stream)
        return 0

    logger.info("%s: %d сирих статей у промпт", stream, len(articles))

    # Живий (редагований через Telegram, 2026-10-03) watchlist
    # (commodities/FX/крипта) + тикери, що пройшли скринінг.
    #
    # **`general` ТЕПЕР ТЕЖ отримує список (2026-10-13)** — раніше
    # лише `watchlist`. Це був справжній дефект, не оптимізація:
    # замір показав **0 із 196** консолідованих історій за 48 годин
    # із заповненим `asset_id`, а без `asset_id` вони не стають
    # каталізатором у торговому списку (`_db.py:
    # fetch_news_catalysts` фільтрує `asset_id IS NOT NULL`). Потік
    # `watchlist` при цьому стояв порожній — GDELT, його єдине
    # джерело з цільовим запитом, у тривалому бані. Тобто список
    # активів передавався рівно в той потік, де статей не було.
    # Та сама правка, що вже зроблена для `run_news_analysis.py`
    # 2026-10-06 із тієї самої причини.
    #
    # `geopolitical` свідомо БЕЗ списку: широкі редакційні стрічки про
    # події, не про активи, і тегування там дало б хибні привʼязки
    # (стаття про вибори — не новина про EUR/USD). Активо-специфічні
    # історії з цього потоку підхоплює детермінований префільтр у
    # `run_news_analysis.py:matched_watchlist_assets`.
    tracked_assets = None
    if stream in ("watchlist", "general"):
        tracked_assets = fetch_asset_ids(conn) + fetch_latest_tickers(conn)

    try:
        saved = consolidate_stream(conn, stream, articles, tracked_assets=tracked_assets)
    except ConsolidationResponseError:
        logger.exception("%s: некоректна відповідь LLM — потік пропущено", stream)
        return 0
    except LlmError:
        logger.exception("%s: збій виклику LLM — потік пропущено", stream)
        return 0

    # Позначаємо ВСІ вхідні статті як враховані (навіть ті, що LLM
    # відкинув як шум) — інакше вони пережовувались би щоразу.
    mark_consolidated(conn, [a["id"] for a in articles])
    # `consolidate_stream` повертає СПИСОК збережених історій, не
    # кількість — лічильник тут мусить брати len() (жива помилка
    # першого прогону після злиття джоб: "+=: 'int' and 'list'").
    logger.info("%s: збережено %d консолідованих історій", stream, len(saved))
    return len(saved)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stream", choices=["watchlist", "general", "geopolitical"], default=None,
        help="конкретний потік; БЕЗ цього прапорця обробляються ВСІ три "
             "(так ходить джоба news_consolidate з 2026-10-13)",
    )
    parser.add_argument(
        "--max-age-days", type=int, default=2,
        help="2 (48г) узгоджено з common/news_db.py:RETENTION_HOURS — старіше все одно не зберігається",
    )
    parser.add_argument("--limit", type=int, default=60, help="стеля на розмір одного LLM-промпту")
    args = parser.parse_args()

    require_api_key("news_consolidation")

    streams = [args.stream] if args.stream else list(STREAMS)

    conn = get_connection()
    try:
        total = 0
        failed: list[str] = []
        for stream in streams:
            try:
                total += consolidate_one_stream(
                    conn, stream, args.max_age_days, args.limit,
                )
            except Exception:
                # Ізоляція потоків: збій на одному не повинен забирати
                # решту (раніше це гарантувалось трьома окремими
                # джобами — тепер мусить гарантувати код).
                logger.exception("%s: несподіваний збій — потік пропущено", stream)
                failed.append(stream)
                conn.rollback()
        logger.info(
            "Готово: %d історій по %d потоках%s",
            total, len(streams),
            f", провалено: {', '.join(failed)}" if failed else "",
        )
        if failed and len(failed) == len(streams):
            # Усі потоки впали — це справжній провал, runner.py мусить
            # ретраїти й сповістити.
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
