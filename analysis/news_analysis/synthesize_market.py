#!/usr/bin/env python3
"""
LLM-синтез глобального контексту (docs/decisions.md, "Ціль 4" —
де зараз великий капітал): risk-on/risk-off висновок із найбільш
підтверджених geopolitical/general історій за вікно + власного
макро-контексту (дохідності/ставки/долар — уже живі FRED/ECB/BOJ ряди).

Відрізняється від synthesize.py (Ціль 1): там LLM зіставляв новинний
сигнал ОДНОГО активу з його ж ціною; тут geopolitical/general новини
не прив'язані до активу (asset_id=NULL, docs/decisions.md, 2026-09-25)
— aggregate_by_asset() тут не застосовний, замість нього
aggregate.py:top_clusters() (найбільш підтверджені історії, не по
активу).

Межа шарів / формат виходу — той самий принцип, що synthesize.py;
провайдер, розбір відповіді й аудит-лог виклику — спільні
(`llm_common.py`), тут лишається лише своє: SYSTEM_PROMPT,
build_prompt() і макро-контекст.

**Сесійний бриф ДО відкриття (2026-10-22, пункт 5 плану сесії).**
Прогін — за `config.SESSION_PREOPEN_MINUTES` до відкриття біржі; вікно
новин — від попередньої сесії (`MARKET_SYNTHESIS_SESSION_HOURS`), а не 7
днів, через які три сесії доби бачили одну вибірку; у промпт іде
список активів для торгів, що торгуються в цю сесію (`trading_list`).

Використання:
    python synthesize_market.py --session us
    python synthesize_market.py --session us --dry-run   # промпт без ШІ
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
import requests

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ANALYSIS_DIR)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))

from common.db import get_connection, fetch_recent  # noqa: E402
from llm_common import (  # noqa: E402
    LlmError,
    SynthesisResponseError,
    call_llm,
    log_llm_call,
    parse_synthesis_response,
    require_api_key,
)
from news_analysis._db import (  # noqa: E402
    fetch_consolidated_as_clusters,
    fetch_session_trading_list,
    save_market_synthesis,
)
from news_analysis.aggregate import NewsCluster  # noqa: E402
from config import (  # noqa: E402
    MARKET_SYNTHESIS_FALLBACK_HOURS,
    MARKET_SYNTHESIS_MAX_STORIES,
    MARKET_SYNTHESIS_MIN_SOURCES,
    MARKET_SYNTHESIS_MIN_STORIES,
    MARKET_SYNTHESIS_SESSION_HOURS,
    SESSION_ASSET_CLASSES,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Не CLI-прапорець — сама суть Цілі 4 саме ці два потоки (asset_id=NULL,
# не прив'язані до активу, на відміну від watchlist).
STREAMS = ("geopolitical", "general")

# Ярлик → (source, metric_id) у raw_observations. Фіксований список,
# дублює стиль reporting/telegram_notify.py:_METRIC_SOURCE (без прямого
# імпорту з monitoring/ — контейнерна незалежність).
MACRO_CONTEXT_METRICS: dict[str, tuple[str, str]] = {
    "US 10Y Treasury Yield": ("fred", "treasury_10y"),
    "US 2Y Treasury Yield": ("fred", "treasury_2y"),
    "US Fed Funds Rate": ("fred", "fed_funds_rate"),
    "EUR/USD": ("fred", "eurusd"),
    "USD/JPY": ("fred", "usdjpy_fx_rate"),
    "Eurozone Deposit Rate": ("ecb", "eurozone_deposit_rate"),
    "Japan Policy Rate": ("boj", "japan_policy_rate"),
}

# Торгові сесії (спек користувача 2026-09-28: "після відкриття
# азіатської/європейської/американської сесій, 3 рази/добу за часом
# сесій, не 1 раз/добу у фіксовану годину").
#
# Навіщо сесія в ПРОМПТІ, а не лише в розкладі: той самий набір новин
# і макро-контексту читається інакше залежно від того, який ринок щойно
# відкрився. Азія вранці реагує на вчорашнє закриття США; Нью-Йорк
# відкривається вже знаючи, що зробила Європа. Без цього три прогони за
# добу дали б три майже однакові висновки з однакових даних.
SESSIONS = {
    "asia": (
        "За ~45 хв відкривається АЗІАТСЬКА сесія (Токіо). Азійські ринки "
        "першими реагують на те, що сталось за ніч, — насамперед на "
        "закриття США й нічні новини. Оціни, з чим ринок підходить до "
        "відкриття і що може задати тон."
    ),
    "europe": (
        "За ~45 хв відкривається ЄВРОПЕЙСЬКА сесія (Лондон). Європа "
        "відкривається, вже бачачи результат азіатської сесії, і додає "
        "власний потік даних та новин ЄЦБ. Оціни, з чим ринок підходить "
        "до відкриття."
    ),
    "us": (
        "За ~45 хв відкривається АМЕРИКАНСЬКА сесія (Нью-Йорк) — найбільший "
        "обсяг торгів доби. США відкриваються, знаючи і Азію, і Європу; "
        "саме тут найчастіше формується домінантний напрямок дня. Оціни, з "
        "чим ринок підходить до відкриття."
    ),
}

DEFAULT_SESSION = "daily"

SYSTEM_PROMPT = (
    "Ти макро-стратег. Тобі дають дві речі: (1) макро-контекст — "
    "останні значення ключових ставок/дохідностей/валютних пар "
    "(поточне і попереднє, щоб бачити напрямок), (2) НАЙБІЛЬШ "
    "ПІДТВЕРДЖЕНІ геополітичні й загальноринкові новинні історії за "
    "вікно (дублікати з різних видань уже об'єднані, відсортовано за "
    "кількістю незалежних джерел — природна міра важливості)."
    "\n\n"
    "Твоя задача — risk-on чи risk-off, спираючись на ОБИДВА входи "
    "РАЗОМ: новини БЕЗ підтвердження макро-контекстом (наприклад, "
    "стаття каже про ескалацію, а дохідності/долар не рухаються) "
    "важать менше, ніж ті, що узгоджуються з фактичним рухом ставок/"
    "валют. direction: up (risk-on — капітал у ризикові активи), down "
    "(risk-off — втеча в безпечні активи), neutral (збалансовано, "
    "немає домінантного напрямку), unclear (немає чіткого сигналу)."
    "\n\n"
    "НІКОЛИ не давай прямих торгових рекомендацій ('купити'/'продати'/"
    "'входити в позицію') — тільки описові характеристики. Відповідай "
    "ЛИШЕ JSON-об'єктом з полями: direction (одне з: up, down, neutral, "
    "unclear), confidence (число від 0 до 1 — наскільки новини й "
    "макро-контекст узгоджуються), summary (1-2 речення: який стан "
    "ринку і чому), reasoning (коротке обґрунтування для аудиту)."
)


def fetch_macro_context(conn) -> dict[str, dict]:
    """Останнє й попереднє значення кожної метрики з
    MACRO_CONTEXT_METRICS — best-effort, метрика без достатньої історії
    просто пропускається (не блокує решту)."""
    context = {}
    for label, (source, metric_id) in MACRO_CONTEXT_METRICS.items():
        observations = fetch_recent(conn, source, metric_id, limit=2)
        if not observations:
            continue
        entry = {
            "latest_value": str(observations[0]["value"]),
            "latest_date": str(observations[0]["observed_at"]),
        }
        if len(observations) > 1:
            entry["previous_value"] = str(observations[1]["value"])
            entry["previous_date"] = str(observations[1]["observed_at"])
        context[label] = entry
    return context


def _cluster_from_consolidated(row: dict) -> NewsCluster:
    """Рядок `news_consolidated` → `NewsCluster`, щоб промпт і
    збереження лишились без змін (контракт `save_market_synthesis`
    очікує саме кластери). `summary` консолідованої історії вже є
    представницьким текстом — окремого заголовка там немає."""
    summary = row["summary"] or ""
    return NewsCluster(
        representative_title=summary,
        source_count=row["source_count"] or 1,
        asset_ids=[row["asset_id"]] if row.get("asset_id") else [],
        dominant_direction=row["direction"] or "unclear",
        direction_counts={},
        summaries=[summary],
        urls=list(row.get("source_urls") or []),
        earliest_published_at=row["created_at"],
        latest_published_at=row["created_at"],
        raw_news_ids=list(row.get("source_raw_news_ids") or []),
    )


def build_prompt(
    clusters: list[NewsCluster], macro: dict, session: str = DEFAULT_SESSION,
    trading: list[dict] | None = None,
) -> str:
    lines = []
    session_context = SESSIONS.get(session)
    if session_context:
        lines.append(session_context)
        lines.append("")
    lines.append("Макро-контекст:")
    if macro:
        for label, entry in macro.items():
            if "previous_value" in entry:
                lines.append(
                    f"- {label}: {entry['latest_value']} ({entry['latest_date']}), "
                    f"попереднє {entry['previous_value']} ({entry['previous_date']})"
                )
            else:
                lines.append(f"- {label}: {entry['latest_value']} ({entry['latest_date']})")
    else:
        lines.append("- немає даних")

    lines.append("")
    lines.append(f"Найбільш підтверджені історії за вікно ({len(clusters)}):")
    for c in clusters:
        lines.append(f"- [{c.source_count} джерел, напрямок {c.dominant_direction}] {c.representative_title}")
        if c.summaries:
            lines.append(f"  → {c.summaries[0]}")

    if trading is not None:
        lines.append("")
        if trading:
            lines.append(
                "Активи, які наш детермінований фільтр відібрав для торгів у цю "
                "сесію (власна подія сьогодні + однозначний напрямок):"
            )
            for item in trading:
                lines.append(f"- {item.get('label') or item['asset_id']} ({item['kind']}), "
                             f"напрямок {item['direction']}: {item.get('why') or ''}")
            lines.append(
                "У summary коротко скажи, чи загальний стан ринку ПІДТРИМУЄ ці "
                "напрямки чи суперечить їм."
            )
        else:
            lines.append("Активів для торгів у цю сесію наш фільтр не відібрав.")
    return "\n".join(lines)


def synthesize_market(
    clusters: list[NewsCluster], macro: dict,
    session: str = DEFAULT_SESSION,
    trading: list[dict] | None = None,
):
    """Той самий контракт, що synthesize.synthesize_asset(): повертає
    (результат, промпт, відповідь_LLM) для обов'язкового логування
    (rule 5)."""
    prompt = build_prompt(clusters, macro, session=session, trading=trading)
    llm = call_llm(prompt, SYSTEM_PROMPT, purpose="market_context_synthesis")
    result = parse_synthesis_response(llm.text)
    return result, prompt, llm


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--session", choices=sorted(SESSIONS), default=None,
        help="торгова сесія, що щойно відкрилась (asia/europe/us) — додає "
             "контекст у промпт і дає ОКРЕМИЙ рядок на добу; без неї "
             "прогін зберігається як 'daily' (сумісність зі старим "
             "одноразовим розкладом)",
    )
    parser.add_argument(
        "--max-age-hours", type=int, default=None,
        help="вікно історій (дефолт — config.MARKET_SYNTHESIS_SESSION_HOURS, "
             "з розширенням до FALLBACK, якщо історій замало)",
    )
    parser.add_argument("--dry-run", action="store_true", help="надрукувати промпт без виклику ШІ")
    parser.add_argument(
        "--top", type=int, default=None,
        help="СТЕЛЯ кількості історій (дефолт — config.MARKET_SYNTHESIS_MAX_STORIES); "
             "не фіксована кількість: беруться всі підтверджені історії до цієї межі",
    )
    parser.add_argument(
        "--min-sources", type=int, default=None,
        help="скільком незалежним джерелам має підхопити історію "
             "(дефолт — config.MARKET_SYNTHESIS_MIN_SOURCES)",
    )
    args = parser.parse_args()

    session = args.session or DEFAULT_SESSION

    if not args.dry_run:
        require_api_key("market_context_synthesis")

    conn = get_connection()
    try:
        # Вхід — КОНСОЛІДОВАНІ історії, не перекластеризовані статті
        # (2026-10-13, повне обґрунтування — `_db.py:
        # fetch_consolidated_as_clusters` і config розділ 14).
        min_sources = args.min_sources or MARKET_SYNTHESIS_MIN_SOURCES
        cap = args.top or MARKET_SYNTHESIS_MAX_STORIES
        hours = args.max_age_hours or MARKET_SYNTHESIS_SESSION_HOURS
        rows = fetch_consolidated_as_clusters(conn, tuple(STREAMS), hours, min_sources, cap)
        if len(rows) < MARKET_SYNTHESIS_MIN_STORIES and not args.max_age_hours:
            # Тихий проміжок між сесіями — розширюємо вікно, але ЯВНО:
            # інакше синтез мовчки писав би висновок із 1-2 історій.
            logger.info(
                "За %d год лише %d підтверджених історій — розширюю вікно до %d год",
                hours, len(rows), MARKET_SYNTHESIS_FALLBACK_HOURS,
            )
            hours = MARKET_SYNTHESIS_FALLBACK_HOURS
            rows = fetch_consolidated_as_clusters(conn, tuple(STREAMS), hours, min_sources, cap)
        top = [_cluster_from_consolidated(row) for row in rows]
        logger.info(
            "Підтверджених історій (>=%d джерел) за %d год: %d (стеля %d)",
            min_sources, hours, len(top), cap,
        )

        if not top:
            logger.info(
                "Немає історій, підтверджених %d+ джерелами, за вікно — "
                "нічого синтезувати (це НЕ збій: підтвердженість і є шкалою "
                "значущості)", min_sources,
            )
            return

        macro = fetch_macro_context(conn)
        logger.info("Макро-контекст: %d показників", len(macro))
        trading = (
            fetch_session_trading_list(conn, SESSION_ASSET_CLASSES[session])
            if session in SESSION_ASSET_CLASSES else None
        )
        if trading is not None:
            logger.info("Активів для торгів у сесію %s: %d", session, len(trading))

        if args.dry_run:
            print(build_prompt(top, macro, session=session, trading=trading))
            logger.info("--dry-run: ШІ не викликався, у БД не записано")
            return

        try:
            result, prompt, llm = synthesize_market(
                top, macro, session=session, trading=trading,
            )
        except SynthesisResponseError:
            logger.exception("Некоректна відповідь LLM — синтез пропущено")
            return
        except LlmError:
            logger.exception("Збій виклику LLM (обидва провайдери) — синтез пропущено")
            return

        llm_call_id = log_llm_call(
            conn,
            llm=llm,
            purpose="market_context_synthesis",
            prompt=prompt,
            source_ref=session,
        )
        save_market_synthesis(
            conn,
            clusters=top,
            macro=macro,
            window_days=max(1, -(-hours // 24)),
            result=result,
            llm_call_id=llm_call_id,
            session=session,
        )
        logger.info(
            "Готово [%s]: direction=%s confidence=%.2f: %s",
            session, result.direction, result.confidence, result.summary,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
