#!/usr/bin/env python3
"""
LLM-синтез "новини + ціна" по активу (docs/decisions.md, "Ціль 1" —
точки входу для watchlist-пар): зводить уже готовий новинний сигнал
(aggregate.py:AssetSignal) з ціновим рухом за те саме вікно
(prices.py:PriceChange) в один причинний висновок — чи рух ціни
пояснюється новинами (схоже на тренд), чи новин немає/суперечать
(схоже на шум/корекцію).

Межа шарів (rule 1, CLAUDE.md): тут вирішується, ЩО означає зіставлення
факту й новин — aggregate.py/prices.py лишаються звичайним кодом без
LLM (analysis/CLAUDE.md).

Формат виходу — analysis/CLAUDE.md "Формат виходу LLM-аналізу":
summary/direction/confidence/reasoning від LLM, + власне поле
confirmation_factors (2026-10-02 — що конкретно підтвердило б чи
спростувало гіпотезу тренд/корекція з summary, НЕ прогноз ціни; тому
свій дрібний контракт/parse_price_news_response(), не спільний
llm_common.parse_synthesis_response()); source_refs — той самий
принцип, що relevance_filter.py: DeepSeek його не генерує (ризик
галюцинації того, що й так відоме викликачу з `signal.summaries`), код
приєднує сам.

Провайдер і аудит-лог виклику — спільні для всіх LLM-скриптів
analysis/ (`llm_common.py`); розбір відповіді тут свій (контракт
відрізняється від synthesize_market.py/expectations/synthesize.py).

Обсяг і ТРИГЕР синтезу (змінено 2026-10-02, живий фідбек користувача):
раніше синтезувався ЛИШЕ актив, що мав новинний сигнал — рух ціни без
жодної новини НІКОЛИ не перевірявся взагалі, навіть дуже різкий.
Тепер: універсум = watchlist (common/watchlist_db.py:fetch_asset_ids,
2026-10-03 — раніше news/queries.py:WATCHLIST_ASSET_IDS) +
останній скринінг акцій (screening._results_db.fetch_latest_tickers)
+ усе, що вже має новинний сигнал; актив синтезується, якщо є НОВИННИЙ
СИГНАЛ (як і раніше, будь-якої сили — фільтр релевантності вже в
relevance_filter.py/aggregate.py, тут нічого не зламано) **або** рух
ціни АНОМАЛЬНИЙ для цього активу (prices.py:is_anomalous_move() —
відносно власної історичної волатильності, не фіксований %). Коли
новин немає — в LLM йде "порожній" сигнал (0 історій), а SYSTEM_PROMPT
явно каже: поясни рух без новинної причини, це вже сама по собі
інформація (технічний рух/ще не висвітлена новина).

Ціна — prices.py:ASSET_PRICE_SOURCES (watchlist-товари/форекс/крипта;
крипта — 2026-10-02) або здогад _resolve_price_source() за twelvedata-
конвенцією (тикери акцій зі скринінгу, 2026-09-27). Актив без ціни
(джерела нема чи застаріла) синтезуватись не може в будь-якому
випадку — пропускається з логом, як і раніше.

Використання:
    python synthesize.py
    python synthesize.py --stream watchlist --max-age-days 3
"""

import argparse
import logging
import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv
import requests

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Корінь репозиторію — для ЄДИНОГО `config.py` (рішення
# користувача 2026-10-04: один конфіг на весь агент).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _ANALYSIS_DIR)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))

from common.db import get_connection  # noqa: E402
from llm_common import (  # noqa: E402
    LlmError,
    DIRECTIONS,
    SynthesisResponseError,
    call_llm,
    log_llm_call,
    parse_confidence,
    parse_json_object,
    require_api_key,
)
import change_gate  # noqa: E402
from change_gate import Anchor  # noqa: E402
from common import market_hours  # noqa: E402
from common.watchlist_db import fetch_asset_ids, fetch_price_sources, fetch_watchlist  # noqa: E402
from config import CHANGE_GATE_NEWS_PRICE_PP  # noqa: E402
from news_analysis._db import fetch_relevant_for_aggregation, save_synthesis  # noqa: E402
from news_analysis.aggregate import AssetSignal, aggregate_by_asset, cluster_articles  # noqa: E402
from news_analysis.prices import (  # noqa: E402
    PriceChange,
    fetch_all_price_changes,
    fetch_latest_observed_at,
    fetch_price_history,
    is_anomalous_move,
)
from screening._results_db import fetch_latest_tickers  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Власний формат виходу (НЕ llm_common.parse_synthesis_response/
# SynthesisResult — той контракт спільний із synthesize_market.py й
# expectations/synthesize.py, і зміна його зачепила б обидва; той самий
# принцип, що relevance_filter.py:NewsAnalysisResult). Додано
# confirmation_factors (2026-10-02, живий фідбек користувача): "ціна
# впала на X%, новини +4" саме собою нічого не пояснює — не було ні
# явної гіпотези тренд/корекція З ЧОГО вона випливає, ні того, що
# перевірити, щоб гіпотезу підтвердити чи спростувати. direction/
# confidence лишаються (summary тепер явно вимагає період+причину+
# гіпотезу, reasoning — аудит, як і раніше).
PRICE_NEWS_FIELDS = ("direction", "confidence", "summary", "confirmation_factors", "reasoning")


@dataclass
class PriceNewsSynthesisResult:
    direction: str
    confidence: float
    summary: str
    confirmation_factors: str
    reasoning: str


def parse_price_news_response(raw_content: str) -> PriceNewsSynthesisResult:
    data = parse_json_object(raw_content, SynthesisResponseError)

    missing = [f for f in PRICE_NEWS_FIELDS if f not in data]
    if missing:
        raise SynthesisResponseError(f"У відповіді LLM бракує полів {missing}: {data!r}")

    direction = data["direction"]
    if direction not in DIRECTIONS:
        raise SynthesisResponseError(f"Неочікуване значення direction: {direction!r}")

    return PriceNewsSynthesisResult(
        direction=direction,
        confidence=parse_confidence(data["confidence"], SynthesisResponseError),
        summary=data["summary"],
        confirmation_factors=data["confirmation_factors"],
        reasoning=data["reasoning"],
    )


SYSTEM_PROMPT = (
    "Ти фінансовий аналітик. Тобі дають один актив: агрегований "
    "новинний сигнал за останні дні (скільки НЕЗАЛЕЖНИХ історій "
    "(дублікати з різних видань уже об'єднані), який напрямок у "
    "кожної, короткі факти з них) і фактичну зміну ціни цього активу "
    "за той самий період (з точними датами)."
    "\n\n"
    "Твоя задача — причинна атрибуція й гіпотеза, за прикладом: 'ціна "
    "впала на X% за [період], на це вплинула [конкретна подія з "
    "новин]; але зважаючи на [що саме] це більше виглядає як корекція "
    "(чи тренд); для підтвердження потрібні [конкретні фактори]'. "
    "Тобто: (1) яка величина руху й за який період, (2) яка конкретна "
    "причина з новин, якщо вона є, (3) гіпотеза — це більше схоже на "
    "фундаментально обґрунтований ТРЕНД (новини пояснюють рух, "
    "напрямки збігаються) чи на ШУМ/КОРЕКЦІЮ (новин немає, вони "
    "суперечать руху ціни, чи занадто нечіткі), (4) ЩО КОНКРЕТНО "
    "підтвердило б чи спростувало цю гіпотезу надалі (напр. 'якщо рух "
    "продовжиться без нових новин — це вже не корекція', 'варто "
    "дивитись на наступний звіт про запаси/ставку') — це список "
    "факторів для спостереження, НЕ прогноз ціни. Кількість "
    "незалежних історій — природна міра сили сигналу: одна стаття "
    "важить менше, ніж та сама новина в кількох джерелах."
    "\n\n"
    "Якщо новин про актив ЗА ЦЕЙ ПЕРІОД немає взагалі (0 історій), а "
    "рух ціни все одно тобі дали — це означає, що рух визнано "
    "НЕЗВИЧНИМ для цього активу (за його власною історичною "
    "волатильністю), просто без видимої новинної причини. Прямо так і "
    "скажи в summary ('новин, що пояснюють цей рух, не знайдено') — "
    "direction=unclear, а в confirmation_factors зазнач, що варто "
    "перевірити (напр. чи з'явиться пояснення найближчими днями, чи "
    "рух сам відкотиться без причини — технічний)."
    "\n\n"
    "НІКОЛИ не давай прямих торгових рекомендацій ('купити'/'продати'/"
    "'входити в позицію') — тільки описові характеристики. Відповідай "
    "ЛИШЕ JSON-об'єктом з полями: direction (одне з: up, down, neutral, "
    "unclear — твоя оцінка напрямку РИЗИКУ для активу з урахуванням "
    "обох входів, не просто напрямок ціни), confidence (число від 0 до "
    "1 — наскільки новини й ціна узгоджуються), summary (2-3 речення: "
    "пункти 1-3 вище — величина руху за період, причина, гіпотеза), "
    "confirmation_factors (1-2 речення: пункт 4 вище — конкретні "
    "фактори/дані для подальшого спостереження), reasoning (коротке "
    "обґрунтування для аудиту)."
)


def build_prompt(asset_id: str, signal: AssetSignal, price: PriceChange) -> str:
    if signal.cluster_count == 0:
        # 2026-10-02: явний рядок замість дампу порожнього
        # direction_counts ({up:0, down:0, ...}) — LLM має одразу
        # побачити "новин немає", не вгадувати це з нулів.
        news_line = "Новинний сигнал за вікно: новин про цей актив не знайдено."
    else:
        news_line = (
            f"Новинний сигнал за вікно: {signal.cluster_count} незалежних історій, "
            f"напрямки {dict(signal.direction_counts)}, net_lean={signal.net_lean:+d}"
        )
    lines = [
        f"Актив: {asset_id}",
        news_line,
        f"Зміна ціни за той самий період ({price.start_date} → {price.end_date}): "
        f"{price.pct_change:.2f}% ({price.start_value} → {price.end_value})",
    ]
    if signal.summaries:
        lines.append("Факти з новин:")
        for s in signal.summaries[:10]:
            lines.append(f"- {s}")
    return "\n".join(lines)


_DIRECTIONS_FOR_EMPTY_SIGNAL = {"up": 0, "down": 0, "neutral": 0, "unclear": 0}


def _empty_signal(asset_id: str) -> AssetSignal:
    """AssetSignal-заглушка для активу без жодної новини за вікно —
    дозволяє прогнати його крізь ту саму build_prompt()/LLM-синтез,
    коли триггер — аномальний рух ціни (is_anomalous_move()), не
    новини (2026-10-02, живий фідбек користувача)."""
    return AssetSignal(
        asset_id=asset_id, cluster_count=0,
        direction_counts=dict(_DIRECTIONS_FOR_EMPTY_SIGNAL), net_lean=0, summaries=[],
    )


def synthesize_asset(asset_id: str, signal: AssetSignal, price: PriceChange):
    """Будує промпт → LLM → парсить. Повертає (результат, промпт,
    відповідь_LLM) — обов'язкові для логування в llm_call_log
    (rule 5), той самий контракт, що relevance_filter.analyze_article()."""
    prompt = build_prompt(asset_id, signal, price)
    llm = call_llm(prompt, SYSTEM_PROMPT, purpose="news_price_synthesis")
    result = parse_price_news_response(llm.text)
    return result, prompt, llm


def gate_inputs(signal: AssetSignal, price: PriceChange) -> tuple[dict, dict]:
    """Входи для гейту змін (2026-10-24): новини — дослівно (нова
    історія = нові дані), рух ціни за вікно — з допуском у п.п.
    (`config.CHANGE_GATE_NEWS_PRICE_PP`), бо вікно ковзає щодня."""
    stable = {"cluster_count": signal.cluster_count, "summaries": sorted(signal.summaries)}
    anchors = {"pct_change": Anchor(float(price.pct_change), CHANGE_GATE_NEWS_PRICE_PP, relative=False)}
    return stable, anchors


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", choices=["watchlist", "general", "geopolitical"], default=None)
    parser.add_argument("--max-age-days", type=int, default=7)
    parser.add_argument(
        "--force", action="store_true",
        help="синтезувати навіть без змін входів і на закритих ринках",
    )
    args = parser.parse_args()

    require_api_key("news_price_synthesis")

    conn = get_connection()
    try:
        articles = fetch_relevant_for_aggregation(
            conn, stream=args.stream, max_age_days=args.max_age_days
        )
        clusters = cluster_articles(articles)
        signals = aggregate_by_asset(clusters)
        logger.info("%d активів із новинним сигналом", len(signals))

        # Універсум ширший за signals (2026-10-02, живий фідбек
        # користувача) — watchlist + поточний скринінг акцій ЗАВЖДИ
        # перевіряються на аномальний рух ціни, навіть без жодної
        # новини; те, що вже має новинний сигнал, лишається в
        # універсумі так само (той самий принцип, docstring модуля
        # вище "Обсяг і ТРИГЕР синтезу").
        screening_tickers = fetch_latest_tickers(conn)
        watchlist_asset_ids = fetch_asset_ids(conn)
        price_sources = fetch_price_sources(conn)
        universe = sorted(set(signals) | set(watchlist_asset_ids) | set(screening_tickers))
        logger.info(
            "%d активів в універсумі (watchlist=%d, скринінг=%d, з новинним сигналом=%d)",
            len(universe), len(watchlist_asset_ids), len(screening_tickers), len(signals),
        )

        # Вихідні й свята (2026-10-24): синтез лише по активах, чий ринок
        # сьогодні торгується. Субота 24.10 до цієї зміни: 12 синтезів
        # по акціях, нафті, золоту й USD/JPY — і жодного по крипті.
        classes = {row["asset_id"]: row.get("asset_class") for row in fetch_watchlist(conn)}
        if not args.force:
            closed = [a for a in universe if not market_hours.asset_is_open(a, classes)]
            if closed:
                logger.info(
                    "Ринок закрито — пропущено %d активів: %s", len(closed), ", ".join(closed),
                )
            universe = [a for a in universe if a not in set(closed)]

        prices = fetch_all_price_changes(conn, universe, days=args.max_age_days, price_sources=price_sources)

        synthesized = unchanged = 0
        for asset_id in universe:
            signal = signals.get(asset_id) or _empty_signal(asset_id)
            has_news = asset_id in signals

            price = prices.get(asset_id)
            if price is None:
                # 2026-09-29, живий фідбек користувача: "немає джерела" й
                # "джерело є, але застаріло" — не одне й те саме, і мовчати
                # про різницю ховає реальні збої collector'ів (напр.
                # watchlist_prices) за нешкідливим на вигляд "пропущено".
                latest = fetch_latest_observed_at(conn, asset_id, price_sources=price_sources)
                if latest is None:
                    logger.info("%s: немає цінового джерела — ніколи не збиралось", asset_id)
                else:
                    logger.warning(
                        "%s: ціна ЗАСТАРІЛА — останнє значення %s, поза вікном %d днів — пропущено",
                        asset_id, latest, args.max_age_days,
                    )
                continue

            if not has_news:
                # Без новин синтезуємо ЛИШЕ якщо рух ціни аномальний
                # для цього активу (prices.py:is_anomalous_move()) —
                # інакше нема про що писати: ні новини, ні незвичного
                # руху. Раніше такий актив НІКОЛИ навіть не перевірявся.
                history = fetch_price_history(conn, asset_id, price_sources=price_sources)
                if not is_anomalous_move(price.pct_change, history, args.max_age_days):
                    logger.debug(
                        "%s: без новин і рух ціни не аномальний (%.2f%%) — пропущено",
                        asset_id, price.pct_change,
                    )
                    continue
                logger.info(
                    "%s: без новин, але рух ціни АНОМАЛЬНИЙ (%.2f%%) — синтезуємо",
                    asset_id, price.pct_change,
                )

            stable, anchors = gate_inputs(signal, price)
            decision = change_gate.check(conn, "news_price_synthesis", asset_id, stable, anchors)
            if not decision.changed and not args.force:
                logger.info("%s: %s (з %s) — синтез лишається чинним", asset_id, decision.reason, decision.since)
                unchanged += 1
                continue

            try:
                result, prompt, llm = synthesize_asset(asset_id, signal, price)
            except SynthesisResponseError:
                logger.exception("%s: некоректна відповідь LLM — пропущено", asset_id)
                continue
            except LlmError:
                logger.exception("%s: збій виклику LLM — пропущено", asset_id)
                continue

            llm_call_id = log_llm_call(
                conn,
                llm=llm,
                purpose="news_price_synthesis",
                prompt=prompt,
                source_ref=asset_id,
            )
            save_synthesis(
                conn,
                asset_id=asset_id,
                signal=signal,
                price=price,
                window_days=args.max_age_days,
                result=result,
                llm_call_id=llm_call_id,
            )
            change_gate.remember(conn, "news_price_synthesis", asset_id, stable, anchors)
            synthesized += 1
            logger.info(
                "%s → direction=%s confidence=%.2f: %s",
                asset_id, result.direction, result.confidence, result.summary,
            )
    finally:
        conn.close()

    logger.info("Готово: %d активів синтезовано, %d без змін входів", synthesized, unchanged)


if __name__ == "__main__":
    main()
