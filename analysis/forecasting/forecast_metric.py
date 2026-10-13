#!/usr/bin/env python3
"""
Формує й зберігає LLM-прогноз наступного значення одного показника
(llm_forecast.py) — наскрізний шлях "історія з БД → LLM → metric_forecasts"
(docs/archive/plan-journal-2026-10-11.md, Фаза 2 + Фаза 5: "Прогнозування — LLM замість Python-моделі",
рішення користувача 2026-09-28).

Не плутати з analysis/expectations/: там факт порівнюється з РИНКОВИМ
очікуванням (ForexFactory), тут — наш ВЛАСНИЙ прогноз на НАСТУПНИЙ
період, незалежна третя точка зору (`llm_forecast.py`, розділ "Що
свідомо не йде в промпт").

Межа шарів (rule 1, CLAUDE.md): цей файл — лише проводка (БД → промпт →
LLM → гейт правдоподібності → БД). Сам промпт, розбір відповіді й
гейт — чисті функції в `llm_forecast.py`; базові моделі (naive/лінійний
тренд) лишаються в `trend.py` як базова лінія для backtest.

Використання:
    python forecast_metric.py --metric cpi
    python forecast_metric.py --metric nonfarm_payrolls --limit 36
"""

import argparse
import logging
import os
import sys
from typing import Optional

from dotenv import load_dotenv
import requests

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "monitoring"))
sys.path.insert(0, _ANALYSIS_DIR)

from common.db import get_connection, fetch_recent  # noqa: E402
from config import (  # noqa: E402
    FORECAST_HISTORY_POINTS,
    FORECAST_YIELD_CONTEXT_METRICS,
    FORECAST_YIELD_LONG,
    FORECAST_YIELD_SHORT,
)
from llm_common import (  # noqa: E402
    LlmError,
    resolve_route,
    SynthesisResponseError,
    call_llm,
    log_llm_call,
    require_api_key,
)
from metric_sources import ADAPTER_BY_METRIC, DAILY_ADAPTER_BY_METRIC  # noqa: E402

from forecasting._metric_source import resolve_source  # noqa: E402
from forecasting.llm_forecast import (
    production_prompt_variant,  # noqa: E402
    SYSTEM_PROMPT,
    build_prompt,
    describe_metric,
    is_plausible,
    parse_forecast_response,
)
from forecasting._context_db import fetch_series  # noqa: E402
from forecasting._db import save_forecast  # noqa: E402
from forecasting._verdict_db import fetch_verdict  # noqa: E402
from forecasting.market_context import build_snapshot, format_snapshot  # noqa: E402
from forecasting.method_selector import choose_method  # noqa: E402
from forecasting.trend import naive_forecast  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

METHOD = "llm"

# Усі показники календаря релізів (10 FRED + 5 ECB/BOJ/e-Stat) ПЛЮС
# денні серії без календаря (облігації/ставка/USDJPY) — ЄДИНЕ джерело
# істини `monitoring/metric_sources.py`, не свій переписаний перелік
# (інакше новий показник тихо лишиться без прогнозу).
#
# Зміна проти `linear_trend` (було FORECASTABLE_METRICS = {"cpi"}):
# лінійна модель була обмежена одним показником, бо лише на ньому
# пройшов backtest. LLM-прогноз такого обмеження не вимагає — він не
# "модель, підтверджена на серії", а інтерпретація історії, однакова
# для всіх серій; якість міряється окремо (`backtest_llm.py`) і
# впливає на ДОВІРУ до прогнозу, не на право його формувати.
#
# Денні серії додані 2026-10-04 (рішення користувача: "облігації це
# обовязково") — до того `treasury_10y`/`treasury_2y` взагалі не могли
# бути спрогнозовані: `resolve_source()` падав на "невідомий
# metric_id". ДВА РІЗНІ тригери (не один): календарні показники
# прогнозуються подією релізу (`update_forecasts.py`), денні — за
# розкладом (`forecast_daily.py`), бо релізу, що їх тригерив би, не
# існує.
FORECASTABLE_METRICS = frozenset(ADAPTER_BY_METRIC) | frozenset(DAILY_ADAPTER_BY_METRIC)

# Менше точок — прогнозувати нема на чому: LLM побачив би напрямок, але
# не "типовий крок" чи сезонність, на які сам промпт просить спиратись
# (і гейт `is_plausible` на такій історії теж не працює,
# llm_forecast.py:PLAUSIBILITY_MIN_HISTORY).
MIN_HISTORY = 5


def build_market_context(conn, metric_id: str, as_of) -> Optional[str]:
    """Текстовий блок ринкового контексту для промпта, або None.

    None — коли показника немає в переліку (більшість) чи коли
    дохідностей на цю дату ще не зібрано: порожній блок у промпті
    лише заплутав би модель."""
    if metric_id not in FORECAST_YIELD_CONTEXT_METRICS:
        return None

    short = fetch_series(conn, *FORECAST_YIELD_SHORT)
    long = fetch_series(conn, *FORECAST_YIELD_LONG)
    if not short and not long:
        logger.warning(
            "%s: дохідностей для ринкового контексту ще немає — прогноз без нього",
            metric_id,
        )
        return None
    return format_snapshot(build_snapshot(short, long, as_of))


def _save_naive_forecast(
    conn, source: str, metric_id: str, choice, values: list[float],
    based_on_observed_at, periods_ahead: int,
) -> Optional[int]:
    """Прогноз БЕЗ виклику LLM — останнє значення без змін
    (`trend.naive_forecast`).

    Пишеться в ту саму таблицю й тим самим шляхом, що LLM-прогноз, але
    з іншим `method` і БЕЗ `confidence`/`llm_call_id`: вигадувати
    впевненість для арифметики було б неправдою, а `reasoning` несе
    ПРИЧИНУ вибору методу — саме її користувач має побачити в
    Telegram замість зниклого ШІ-прогнозу.

    `direction='neutral'` — прогноз дорівнює поточному значенню, тож
    напрямку немає за визначенням (словник напрямків —
    llm_common.DIRECTIONS)."""
    forecast_value = naive_forecast(values)
    if forecast_value is None:
        logger.warning("%s: порожня історія — naive-прогноз неможливий", metric_id)
        return None

    forecast_id = save_forecast(
        conn,
        source=source,
        metric_id=metric_id,
        method=choice.method,
        based_on_observed_at=based_on_observed_at,
        periods_ahead=periods_ahead,
        forecast_value=forecast_value,
        direction="neutral",
        confidence=None,
        summary=f"Без змін: {forecast_value:.6g}",
        reasoning=choice.why,
        llm_call_id=None,
    )
    logger.info(
        "%s: прогноз БЕЗ ШІ на %d період(и) від %s = %.6g (метод %s, id=%d)",
        metric_id, periods_ahead, based_on_observed_at, forecast_value,
        choice.method, forecast_id,
    )
    return forecast_id


def run_forecast(
    conn, metric_id: str, periods_ahead: int = 1, limit: int = FORECAST_HISTORY_POINTS
) -> Optional[int]:
    """Формує й зберігає LLM-прогноз для одного показника. Спільна
    логіка між CLI (main(), ручний прогін) і update_forecasts.py
    (автотригер одразу після того, як monitoring задетектував новий
    реліз). None — прогноз не збережено (замало історії, збійна
    відповідь LLM або неправдоподібне число); причина — у лозі."""
    source = resolve_source(metric_id)

    observations = fetch_recent(conn, source, metric_id, limit=limit)
    if len(observations) < MIN_HISTORY:
        logger.warning(
            "%s: замало історії (%d, потрібно >= %d) для прогнозу",
            metric_id, len(observations), MIN_HISTORY,
        )
        return None

    chronological = list(reversed(observations))
    values = [float(o["value"]) for o in chronological]
    based_on_observed_at = chronological[-1]["observed_at"]

    # ВИБІР МЕТОДУ (P0, 2026-10-05) — до цього LLM викликався для ВСІХ
    # 19 показників однаково, і 4 з них стабільно надсилали прогноз,
    # гірший за безкоштовне "нічого не змінилось", а 2 ряди не
    # рухались жодного разу. Вердикт читається для МОДЕЛІ, якою
    # прогноз реально робиться (`config.py:LLM_ROUTES`).
    route = resolve_route("metric_forecast")
    stored = fetch_verdict(
        conn, metric_id, route.model, prompt_variant=production_prompt_variant(metric_id)
    )
    choice = choose_method(
        values,
        verdict=stored["verdict"] if stored else None,
        advantage_pct=float(stored["advantage_pct"]) if stored and stored["advantage_pct"] is not None else None,
        verdict_model=stored["model"] if stored else None,
    )
    logger.info("%s: метод %s — %s", metric_id, choice.method, choice.why)

    if not choice.use_llm:
        return _save_naive_forecast(
            conn, source, metric_id, choice, values, based_on_observed_at, periods_ahead
        )

    # Ринковий контекст (дохідності 2Y/10Y + спред) — лише показникам
    # із `config.FORECAST_YIELD_CONTEXT_METRICS`, станом на ТУ САМУ
    # дату, що остання точка історії (2026-10-11).
    market_context = build_market_context(conn, metric_id, based_on_observed_at)
    prompt = build_prompt(
        metric_id, describe_metric(metric_id), chronological,
        market_context=market_context,
    )
    llm = call_llm(prompt, SYSTEM_PROMPT, purpose="metric_forecast")
    raw_content = llm.text
    result = parse_forecast_response(raw_content)

    # Аудит-лог виклику — ОБОВ'ЯЗКОВО до гейта нижче (rule 5,
    # CLAUDE.md): саме відкинутий прогноз найцікавіше розбирати
    # постфактум, і без запису в llm_call_log від нього не лишилось би
    # і сліду.
    llm_call_id = log_llm_call(
        conn,
        llm=llm,
        purpose="metric_forecast",
        prompt=prompt,
        source_ref=f"{source}:{metric_id}:{based_on_observed_at}",
    )

    plausible, reason = is_plausible(values, result.forecast_value)
    if not plausible:
        logger.error(
            "%s: прогноз ВІДКИНУТО як неправдоподібний — %s (llm_call_id=%d)",
            metric_id, reason, llm_call_id,
        )
        return None

    forecast_id = save_forecast(
        conn,
        source=source,
        metric_id=metric_id,
        method=choice.method,
        based_on_observed_at=based_on_observed_at,
        periods_ahead=periods_ahead,
        forecast_value=result.forecast_value,
        direction=result.direction,
        confidence=result.confidence,
        summary=result.summary,
        reasoning=result.reasoning,
        watch_on_release=result.watch_on_release,
        llm_call_id=llm_call_id,
    )
    logger.info(
        "%s: прогноз на %d період(и) вперед від %s = %.6g (direction=%s "
        "confidence=%.2f, id=%d): %s",
        metric_id, periods_ahead, based_on_observed_at, result.forecast_value,
        result.direction, result.confidence, forecast_id, result.summary,
    )
    return forecast_id


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metric", required=True)
    parser.add_argument("--periods-ahead", type=int, default=1)
    parser.add_argument(
        "--limit", type=int, default=FORECAST_HISTORY_POINTS,
        help="скільки останніх спостережень давати LLM (дефолт — config.py)"
    )
    args = parser.parse_args()

    require_api_key("metric_forecast")

    conn = get_connection()
    try:
        try:
            forecast_id = run_forecast(
                conn,
                args.metric,
                periods_ahead=args.periods_ahead,
                limit=args.limit,
            )
        except SynthesisResponseError:
            # ПЕРЕД ValueError нижче: SynthesisResponseError — підклас
            # ValueError (llm_common.py), і в зворотному порядку
            # загальний handler з'їв би його, залишивши збійну відповідь
            # LLM без власного повідомлення.
            logger.exception("%s: некоректна відповідь LLM", args.metric)
            sys.exit(1)
        except ValueError as e:
            # resolve_source() — невідомий metric_id.
            logger.error(str(e))
            sys.exit(1)
        except LlmError:
            logger.exception("%s: збій виклику LLM", args.metric)
            sys.exit(1)

        if forecast_id is None:
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
