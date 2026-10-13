#!/usr/bin/env python3
"""
Backtest LLM-прогнозу (llm_forecast.py) на історії одного показника —
виконання вимоги analysis/CLAUDE.md "Тестування прогнозних моделей":
кожна модель прогнозування має бути прогоняна на історичних даних, і
критерій "має сенс" — не "працює без помилок", а СТАБІЛЬНО ТОЧНІША за
naive-базову лінію ("нічого не змінилось").

Зміна напрямку на LLM (2026-09-28, рішення користувача) цю вимогу НЕ
скасовує — тільки робить її дорожчою: кожна перевірена точка це окремий
виклик LLM, тому за замовчуванням перевіряється лише `--points 8`
останніх точок (проти `backtest.py`, що безкоштовно проганяє всю
історію лінійною моделлю). Звідси й чесне обмеження висновку: 8 точок —
це індикація, не статистика; великі числа варто набирати свідомо й
рідко.

Честний out-of-sample: на кожній точці в промпт ідуть ЛИШЕ значення ДО
неї, майбутнє не підглядається — той самий принцип, що
`backtest.py:backtest_metric`.

Поруч із LLM міряються ОБИДВІ базові лінії з `trend.py` на тих самих
точках: naive (головний критерій) і лінійний тренд (попередній
напрямок, 2026-09-27) — щоб відповідь на "чи LLM узагалі вартий своїх
грошей" була в одному виводі, без окремого прогону.

Використання:
    python backtest_llm.py --metric cpi
    python backtest_llm.py --metric nonfarm_payrolls --points 12 --min-history 8
"""

import argparse
import logging
import os
import sys
from datetime import date
from typing import Callable, Optional

from dotenv import load_dotenv
import requests

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "monitoring"))
sys.path.insert(0, _ANALYSIS_DIR)

from common.db import get_connection, fetch_recent  # noqa: E402
from llm_common import (  # noqa: E402
    LlmError,
    SynthesisResponseError,
    call_llm,
    log_llm_call,
    require_api_key,
    LlmRoute,
    resolve_route,
)

from forecasting._metric_source import resolve_source  # noqa: E402
from forecasting._context_db import fetch_series  # noqa: E402
from forecasting._verdict_db import save_verdict  # noqa: E402
from forecasting.market_context import build_snapshot, format_snapshot  # noqa: E402
from forecasting.method_selector import classify_backtest  # noqa: E402
from forecasting.llm_forecast import (  # noqa: E402
    PROMPT_VARIANT,
    PROMPT_VARIANT_WITH_DELTA,
    SYSTEM_PROMPT,
    build_prompt,
    describe_metric,
    is_plausible,
    parse_forecast_response,
)
from forecasting.trend import linear_trend_forecast, naive_forecast  # noqa: E402

from config import (  # noqa: E402
    FORECAST_HISTORY_POINTS,
    FORECAST_TREND_BASELINE_POINTS,
    FORECAST_YIELD_LONG,
    FORECAST_YIELD_SHORT,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_POINTS = 8
DEFAULT_MIN_HISTORY = 6


def _mae(errors: list[float]) -> Optional[float]:
    return sum(errors) / len(errors) if errors else None


def backtest_llm_metric(
    metric_id: str,
    observations_desc: list[dict],
    forecast_fn: Callable[[list[dict]], Optional[float]],
    points: int = DEFAULT_POINTS,
    min_history: int = DEFAULT_MIN_HISTORY,
) -> dict:
    """`observations_desc` — результат fetch_recent() (найновіше перше).
    `forecast_fn(chronological_history) -> прогноз або None` —
    інжектується, щоб ця функція була тестована БЕЗ мережі/LLM (той
    самий прийом, що в тестах решти проєкту: чиста логіка окремо від
    проводки).

    Перевіряються `points` ОСТАННІХ точок історії, для яких попередньої
    історії досить (>= min_history). None-прогноз (збійна відповідь
    LLM) не рахується ні як влучення, ні як промах — точка просто
    пропускається, і це видно в `n_skipped`: мовчазне заниження MAE за
    рахунок "незручних" точок було б гіршим за честний пропуск.

    КРИТИЧНО (виправлено 2026-10-04 після живого прогону
    `unemployment_rate`): пропущена точка виключається і з БАЗОВИХ
    ЛІНІЙ. Спершу було навпаки — naive/тренд рахувались на ВСІХ точках
    "щоб порівнювати на тих самих" — і саме це робило порівняння
    нечесним: набори точок ставали РІЗНИМИ (LLM 7, naive 8). Живий
    кейс: naive отримав "безкоштовну" 8-му точку з похибкою 0 (факт
    4.1 = попереднє 4.1), якої LLM не мав, і вердикт перевернувся з
    нічиєї (0.0714 проти 0.0714) на "LLM гірший на 14.3%". MAE
    порівнюються честно лише тоді, коли набір точок ОДНАКОВИЙ для всіх
    трьох моделей."""
    chronological = list(observations_desc[::-1])
    values = [float(o["value"]) for o in chronological]

    first_index = max(min_history, len(values) - points)

    llm_errors: list[float] = []
    naive_errors: list[float] = []
    trend_errors: list[float] = []
    n_skipped = 0

    for i in range(first_index, len(values)):
        history_rows = chronological[:i]
        history_values = values[:i]
        actual = values[i]

        llm_pred = forecast_fn(history_rows)
        if llm_pred is None:
            # Точка не входить НІ в LLM-MAE, НІ в базові лінії —
            # інакше набори точок розійшлись би (див. докстрінг).
            n_skipped += 1
            logger.warning(
                "%s @ %s: прогнозу немає — точка пропущена (і для базових ліній теж)",
                metric_id, chronological[i]["observed_at"],
            )
            continue

        llm_errors.append(abs(actual - llm_pred))
        logger.info(
            "%s @ %s: факт=%.6g LLM=%.6g (|похибка|=%.6g)",
            metric_id, chronological[i]["observed_at"], actual, llm_pred,
            abs(actual - llm_pred),
        )

        naive_pred = naive_forecast(history_values)
        if naive_pred is not None:
            naive_errors.append(abs(actual - naive_pred))
        trend_pred = linear_trend_forecast(
            history_values, window=FORECAST_TREND_BASELINE_POINTS
        )
        if trend_pred is not None:
            trend_errors.append(abs(actual - trend_pred))

    return {
        "metric_id": metric_id,
        "n_points": len(llm_errors),
        "n_skipped": n_skipped,
        "llm_mae": _mae(llm_errors),
        "naive_mae": _mae(naive_errors),
        "trend_mae": _mae(trend_errors),
    }


def _as_date(value):
    """`observed_at` приходить то як `date` (з БД), то як рядок (у
    тестах і фікстурах) — контекст рахується по датах, тож приводимо."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def make_llm_forecast_fn(
    conn,
    metric_id: str,
    route: Optional[LlmRoute] = None,
    include_deltas: bool = False,
    yield_context: bool = False,
) -> Callable[[list[dict]], Optional[float]]:
    """forecast_fn для backtest_llm_metric(): той САМИЙ промпт і той
    самий гейт правдоподібності, що в бойовому forecast_metric.py —
    інакше backtest міряв би не те, що працює в продакшені.

    Кожен виклик пишеться в llm_call_log із purpose
    'metric_forecast_backtest' (rule 5, CLAUDE.md) — окремо від
    бойового 'metric_forecast', щоб backtest-прогони не змішувались з
    реальними при розборі постфактум."""

    short_series = long_series = None
    if yield_context:
        short_series = fetch_series(conn, *FORECAST_YIELD_SHORT)
        long_series = fetch_series(conn, *FORECAST_YIELD_LONG)

    def forecast_fn(history_rows: list[dict]) -> Optional[float]:
        # ТЕ САМЕ вікно історії, що бойовий прогноз
        # (`forecast_metric.run_forecast(limit=FORECAST_HISTORY_POINTS)`).
        # 2026-10-06: доти сюди йшла ВСЯ історія, і після backfill
        # (14 -> 285 точок) бектест став міряти модель на 190 рядках,
        # тоді як продакшен давав 24 — тобто виміряна точність більше
        # не стосувалась того, що працює.
        windowed = history_rows[-FORECAST_HISTORY_POINTS:]
        # Контекст станом на ОСТАННЮ ТОЧКУ ІСТОРІЇ, не на "сьогодні":
        # інакше прогноз на 2015 рік спирався б на дохідності 2026-го,
        # і виміряна точність була б фікцією (заглядання в майбутнє).
        context = None
        if yield_context and short_series is not None:
            context = format_snapshot(
                build_snapshot(short_series, long_series, _as_date(windowed[-1]["observed_at"]))
            )
        prompt = build_prompt(
            metric_id, describe_metric(metric_id), windowed,
            include_deltas=include_deltas, market_context=context,
        )
        try:
            llm = call_llm(
                prompt, SYSTEM_PROMPT, purpose="metric_forecast_backtest", route=route
            )
            raw_content = llm.text
        except LlmError:
            logger.exception("%s: збій виклику LLM", metric_id)
            return None

        log_llm_call(
            conn,
            llm=llm,
            purpose="metric_forecast_backtest",
            prompt=prompt,
            source_ref=f"{metric_id}:{history_rows[-1]['observed_at']}",
        )

        try:
            result = parse_forecast_response(raw_content)
        except SynthesisResponseError:
            logger.exception("%s: некоректна відповідь LLM", metric_id)
            return None

        plausible, reason = is_plausible(
            [float(r["value"]) for r in history_rows], result.forecast_value
        )
        if not plausible:
            logger.error("%s: прогноз відкинуто гейтом — %s", metric_id, reason)
            return None
        return result.forecast_value

    return forecast_fn


def format_verdict(result: dict) -> str:
    """Людський вердикт за критерієм analysis/CLAUDE.md (точніша за
    naive), з чесною поправкою на малу вибірку."""
    llm_mae, naive_mae = result["llm_mae"], result["naive_mae"]
    if llm_mae is None or naive_mae is None:
        return "вердикту немає — жодної перевіреної точки"

    if naive_mae == 0:
        # Naive без жодної похибки (абсолютно плоска серія) — відносна
        # різниця не визначена, тож віддаємо абсолютні числа.
        return (
            "naive безпомилковий на цих точках (серія не змінювалась) — "
            f"LLM MAE {llm_mae:.6g}, порівнювати у відсотках нема з чим"
        )

    diff_pct = (naive_mae - llm_mae) / naive_mae * 100
    # Поріг нічиєї: < 0.05% різниці округлилось би до "0.0%", і вивід
    # "LLM гірший на 0.0%" виглядав би як збій, а не як рівність
    # (живий кейс `unemployment_rate` 2026-10-04 — рівно однакові MAE).
    if abs(diff_pct) < 0.05:
        return "нічия — LLM і naive однаково точні на цих точках"
    if diff_pct > 0:
        return f"LLM точніший за naive на {diff_pct:.1f}%"
    return f"naive кращий — LLM гірший на {abs(diff_pct):.1f}%"


def _variant_label(args) -> str:
    """Назва варіанту промпта для `backtest_verdicts.prompt_variant` —
    щоб два різні виміри не виглядали як один (2026-10-06)."""
    label = PROMPT_VARIANT_WITH_DELTA if args.delta else PROMPT_VARIANT
    if getattr(args, "yield_context", False):
        label += "+yields"
    return label


def resolve_backtest_route(args) -> LlmRoute:
    """Маршрут прогону: з конфіга, але з перекриттям із CLI.

    Потрібно саме бектесту (2026-10-05): він за визначенням міряє
    РІЗНІ моделі на тих самих точках, а правити для цього `config.py`
    означало б заодно перемкнути бойовий прогноз."""
    route = resolve_route("metric_forecast_backtest")
    if not (args.provider or args.model or args.effort):
        return route
    return LlmRoute(
        provider=args.provider or route.provider,
        model=args.model or route.model,
        effort=args.effort or route.effort,
        max_tokens=route.max_tokens,
    )


def _route_label(route: LlmRoute) -> str:
    """Маршрут цього прогону одним рядком. Іде в підсумковий лог: без
    нього дві таблиці бектесту неможливо порівняти (2026-10-05)."""
    label = f"{route.provider}/{route.model}"
    return f"{label}, effort={route.effort}" if route.effort else label


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metric", required=True)
    parser.add_argument(
        "--points", type=int, default=DEFAULT_POINTS,
        help=f"скільки останніх точок перевірити (= стільки викликів LLM, дефолт {DEFAULT_POINTS})",
    )
    parser.add_argument("--min-history", type=int, default=DEFAULT_MIN_HISTORY)
    # Перекриття маршруту ЛИШЕ для цього прогону — щоб зміряти іншу
    # модель на тих самих точках, не чіпаючи бойовий прогноз
    # (config.py розділ 9).
    # Варіант ПРОМПТА як параметр виміру — та сама логіка, що
    # `--model` (2026-10-06): інакше порівняти "з Δ" і "без Δ" можна
    # було б лише правкою коду посеред серії прогонів, а саме це вже
    # двічі зробило таблицю результатів несумісною самою з собою.
    parser.add_argument(
        "--yield-context", action="store_true",
        help="подати в промпт дохідності 2Y/10Y і спред 2s10s "
             "(config.FORECAST_YIELD_CONTEXT_METRICS) — для A/B перевірки",
    )
    parser.add_argument(
        "--delta", action="store_true",
        help="промпт З таблицею кроків Δ (варіант, відкинутий A/B 2026-10-06 — "
             "для повторної перевірки гіпотези, не для продакшену)",
    )
    parser.add_argument("--provider", choices=["deepseek", "anthropic"], default=None)
    parser.add_argument("--model", default=None, help="напр. claude-sonnet-5")
    parser.add_argument(
        "--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None,
        help="глибина розмірковування Anthropic (головний важіль вартості)",
    )
    parser.add_argument(
        "--limit", type=int, default=200, help="скільки останніх спостережень тягнути з БД"
    )
    args = parser.parse_args()

    route = resolve_backtest_route(args)
    require_api_key("metric_forecast_backtest", provider=route.provider)

    try:
        source = resolve_source(args.metric)
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    conn = get_connection()
    try:
        observations = fetch_recent(conn, source, args.metric, limit=args.limit)
        if len(observations) <= args.min_history:
            logger.error(
                "%s: замало історії (%d, потрібно > %d) — backtest неможливий",
                args.metric, len(observations), args.min_history,
            )
            sys.exit(1)

        result = backtest_llm_metric(
            args.metric,
            observations,
            make_llm_forecast_fn(
                conn, args.metric, route=route, include_deltas=args.delta,
                yield_context=args.yield_context,
            ),
            points=args.points,
            min_history=args.min_history,
        )

        if result["n_points"] == 0:
            logger.error(
                "%s: жодної точки не вдалось перевірити (пропущено %d) — результату немає",
                args.metric, result["n_skipped"],
            )
            sys.exit(1)

        # ПІДСУМОК ПЕРШИМ, збереження другим (2026-10-06, живий збій:
        # `japan_cpi` витратив 50 LLM-викликів і не лишив НІЧОГО — на
        # той момент колонка `prompt_variant` ще не була застосована в
        # БД, INSERT упав, і виняток забрав із собою ще й не
        # надрукований результат). Міряння коштує грошей і часу, тож
        # його результат мусить потрапити в лог НЕЗАЛЕЖНО від того, чи
        # вдалось записати вердикт.
        #
        # Провайдер і модель у виводі обовʼязкові (2026-10-04, питання
        # користувача): колонка LLM специфічна для конкретної МОДЕЛІ, а
        # на Anthropic їх дві з різною ціною. naive/лінійний тренд від
        # провайдера не залежать — чиста арифметика на самому ряді.
        logger.info(
            "%s [%s]: %d точок перевірено (пропущено %d), MAE LLM=%.6g, "
            "naive=%.6g, лінійний тренд=%s — %s",
            result["metric_id"], _route_label(route), result["n_points"],
            result["n_skipped"], result["llm_mae"], result["naive_mae"],
            f"{result['trend_mae']:.6g}" if result["trend_mae"] is not None else "n/a",
            format_verdict(result),
        )

        # Вердикт у БД (P0, 2026-10-05): доти результат бектесту лишався
        # ЛИШЕ в лозі, тож бойовий прогноз не мав як дізнатись, що на
        # цьому показнику ШІ гірший за безкоштовну базову лінію — і
        # далі платив за виклик. Тепер `forecast_metric.py` читає саме
        # цей рядок.
        verdict, advantage_pct = classify_backtest(result["llm_mae"], result["naive_mae"])
        try:
            save_verdict(
                conn,
                metric_id=result["metric_id"],
                provider=route.provider,
                model=route.model,
                effort=route.effort,
                n_points=result["n_points"],
                n_skipped=result["n_skipped"],
                llm_mae=result["llm_mae"],
                naive_mae=result["naive_mae"],
                trend_mae=result["trend_mae"],
                verdict=verdict,
                advantage_pct=advantage_pct,
                prompt_variant=_variant_label(args),
            )
        except Exception:
            # НЕ ковтаємо тихо: вихід із кодом 1 після вже
            # надрукованого результату — щоб runner побачив провал, а
            # людина не втратила виміряні числа.
            logger.exception(
                "%s: вердикт НЕ збережено (результат вище лишається дійсним)",
                result["metric_id"],
            )
            sys.exit(1)
        logger.info(
            "%s: вердикт %r (%s) збережено для %s, промпт %s",
            result["metric_id"], verdict,
            f"{advantage_pct:+.1f}%" if advantage_pct is not None else "без відсотків",
            route.model,
            _variant_label(args),
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
