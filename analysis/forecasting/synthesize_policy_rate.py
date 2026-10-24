#!/usr/bin/env python3
"""
ПОВНИЙ СИНТЕЗ СТАВКИ — прогноз РІШЕННЯ засідання, не значення на завтра.

**Постановка користувача 2026-10-11:** "ставка прогнозується після
виходу нових макроекономічних даних + новини + виступи банків та
голів комісій". Це був останній незакритий пункт плану.

**Чому звичайний прогноз тут не працює — і це ЗМІРЯНО.** Ставка стоїть
між засіданнями, тож точковий прогноз "значення завтра" дорівнює
"значення сьогодні" й завжди правий — на `fed_funds_rate` і
`eurozone_deposit_rate` і naive, і ШІ дали нуль помилки
(docs/archive/plan-journal-2026-10-11.md). Тобто бектест формально
показував ідеальний результат, а передбачувальної цінності не було
НІЯКОЇ. Корисне питання інше: що буде НА ЗАСІДАННІ — hold, cut чи
hike.

**П'ять входів (усі вже існували окремо, не зводились разом):**
1. **макродані** — останні значення показників, за якими ЦБ ухвалює
   рішення (інфляція, зайнятість, безробіття);
2. **наші прогнози** цих показників (`metric_forecasts`) — те, що ЦБ
   побачить ДО засідання, а не лише те, що вже вийшло;
3. **дохідності й спред 2s10s** (`market_context.py`) — ціна, яку
   ринок уже заклав; зміряно, що цей вхід дав `mortgage_rate_30y`
   −3.8% → +16.0%;
4. **новини** — консолідований ринковий контекст;
5. **виступи членів комітету** (`policy_speeches`) — позиція
   посадовців ДО засідання. Живий замір 13 жовтня: 9 виступів Fed і 2
   ECB у вікні тижня.

**Вихід — рішення, не число:** `decision` (hold/cut/hike), очікуване
значення ставки після засідання, обґрунтування і "на що звернути
увагу". Для кожного ЦБ окремо (fed/ecb/boj), бо входи різні.

Використання:
    python synthesize_policy_rate.py --bank fed
    python synthesize_policy_rate.py            # усі банки з рішенням у вікні
"""

import argparse
import json
import logging
import os
import sys
from datetime import date
from typing import Optional

from dotenv import load_dotenv

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ANALYSIS_DIR)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "monitoring"))

from common.db import fetch_recent, get_connection  # noqa: E402
from config import (  # noqa: E402
    POLICY_RATE_BANKS,
    POLICY_SPEECH_DAYS_AHEAD,
    POLICY_SPEECH_DAYS_BACK,
    POLICY_SYNTHESIS_HORIZON_DAYS,
)
from config import CHANGE_GATE_YIELD_PP, FORECAST_YIELD_LONG, FORECAST_YIELD_SHORT  # noqa: E402
import change_gate  # noqa: E402
from change_gate import Anchor  # noqa: E402
from forecasting.market_context import build_snapshot, format_snapshot  # noqa: E402
from llm_common import (  # noqa: E402
    LlmError,
    call_llm,
    log_llm_call,
    parse_confidence,
    parse_json_object,
    require_api_key,
)
from speeches import fetch_recent_speeches  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PURPOSE = "policy_rate_synthesis"
DECISIONS = ("hold", "cut", "hike", "unclear")

SYSTEM_PROMPT = (
    "Ти макроаналітик. Тобі дають усі входи, за якими центральний банк "
    "ухвалює рішення по ставці: останні макродані, НАШІ прогнози цих "
    "показників на найближчий реліз, стан кривої дохідностей, ринковий "
    "новинний контекст і заплановані/відбулі виступи членів комітету.\n\n"
    "Твоє завдання — спрогнозувати РІШЕННЯ НАЙБЛИЖЧОГО ЗАСІДАННЯ, не "
    "значення ставки завтра. Між засіданнями ставка не змінюється, тож "
    "питання 'яке буде значення завтра' сенсу не має.\n\n"
    "Не давай інвестиційних порад і не кажи, що робити з активами — "
    "лише що зробить банк і чому.\n\n"
    "Якщо входи суперечать одне одному (напр. дані за зниження, а "
    "виступи — за утримання), СКАЖИ ПРО ЦЕ прямо й познач нижчу "
    "впевненість: суперечність входів — це теж висновок, і для "
    "читача вона цінніша за вдавану визначеність.\n\n"
    "Відповідай ЛИШЕ JSON-об'єктом з полями: decision (одне з: hold, "
    "cut, hike, unclear), expected_rate (ЧИСЛО — очікуване значення "
    "ставки ПІСЛЯ засідання, у тих самих одиницях, що поточне), "
    "confidence (0..1), summary (2-3 речення: що зробить банк і чому), "
    "reasoning (на які саме входи ти спирався і як їх зважив), "
    "watch_on_release (на що звернути увагу в день засідання — яка "
    "деталь рішення чи формулювання покаже, чи прогноз виправдався)."
)


def _yield_series(conn, source: str, metric_id: str) -> list:
    """[(дата, значення)] для `market_context` — той самий вхід, що
    формує контекст дохідностей у прогнозі показників."""
    rows = fetch_recent(conn, source, metric_id, limit=90)
    return [(r["observed_at"], float(r["value"])) for r in reversed(rows)]


def fetch_metric_snapshot(conn, source: str, metric_id: str, points: int = 4) -> list[dict]:
    rows = fetch_recent(conn, source, metric_id, limit=points)
    return list(reversed(rows))


def fetch_forecast(conn, metric_id: str):
    """Наш останній прогноз цього показника — лише зроблений ШІ.

    `naive_*` сюди не йдуть СВІДОМО: їхній "прогноз" дорівнює
    поточному значенню, тобто в промпті це був би той самий рядок
    двічі, поданий як дві незалежні думки."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT metric_id, forecast_value, direction, confidence, summary, method
            FROM metric_forecasts
            WHERE metric_id = %s
              AND method NOT LIKE 'naive%%' AND method <> 'linear_trend'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (metric_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([c[0] for c in cur.description], row))


def fetch_next_meeting(conn, metric_id: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT scheduled_at FROM release_log
            WHERE metric_id = %s AND status = 'pending' AND scheduled_at >= now()
            ORDER BY scheduled_at
            LIMIT 1
            """,
            (metric_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def fetch_market_news(conn, limit: int = 8) -> list[dict]:
    """Останній сесійний синтез ринку + підтверджені історії —
    готовий контекст, без нового LLM-виклику на новини."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT summary, direction, confidence
            FROM market_synthesis
            ORDER BY created_at DESC
            LIMIT 1
            """,
        )
        row = cur.fetchone()
        market = dict(zip([c[0] for c in cur.description], row)) if row else None

        cur.execute(
            """
            SELECT summary, direction, source_count
            FROM news_consolidated
            WHERE source_count >= 2
              AND created_at >= now() - interval '3 days'
            ORDER BY source_count DESC, created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        stories = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
    return {"market": market, "stories": stories}


def build_prompt(conn, bank: str, config_entry: dict, gate: Optional[dict] = None) -> str:
    """`gate` (2026-10-24) — якщо передано словник, у нього кладуться
    входи для гейту змін (`analysis/change_gate.py`): `stable` — те, що
    має збігатися дослівно (дані, прогнози, історії, виступи), `anchors`
    — дохідності 2Y/10Y, що рухаються щодня й порівнюються з допуском.
    Збираються тут же, з тих самих рядків, що йдуть у промпт — щоб гейт
    і промпт не могли розійтись."""
    stable: dict = {"bank": bank}
    anchors: dict = {}
    lines = [
        f"Центральний банк: {config_entry['label']}",
    ]

    meeting = fetch_next_meeting(conn, config_entry["rate_metric"])
    lines.append(
        f"Найближче засідання: {meeting:%Y-%m-%d %H:%M %Z}" if meeting
        else "Найближче засідання: дата ще не заведена в календар"
    )

    stable["meeting"] = str(meeting)
    current = fetch_metric_snapshot(conn, config_entry["rate_source"], config_entry["rate_metric"], 3)
    stable["rate"] = [[str(r["observed_at"]), str(r["value"])] for r in current]
    if current:
        lines.append(
            "Поточна ставка: "
            + ", ".join(f"{r['observed_at']}: {r['value']}" for r in current)
        )
    lines.append("")

    lines.append("1. МАКРОДАНІ (вхід рішення):")
    for source, metric_id, label in config_entry["macro_inputs"]:
        points = fetch_metric_snapshot(conn, source, metric_id)
        stable.setdefault("macro", {})[metric_id] = [
            [str(r["observed_at"]), str(r["value"])] for r in points
        ]
        if not points:
            lines.append(f"- {label}: даних немає")
            continue
        rendered = ", ".join(f"{r['observed_at']}: {r['value']}" for r in points)
        lines.append(f"- {label} — {rendered}")
    lines.append("")

    lines.append("2. НАШІ ПРОГНОЗИ цих показників на найближчий реліз:")
    any_forecast = False
    for _source, metric_id, label in config_entry["macro_inputs"]:
        forecast = fetch_forecast(conn, metric_id)
        if forecast is None:
            continue
        any_forecast = True
        stable.setdefault("forecasts", {})[metric_id] = [
            str(forecast["forecast_value"]), str(forecast["direction"]),
        ]
        lines.append(
            f"- {label}: {forecast['forecast_value']} "
            f"(напрямок {forecast['direction']}, упевненість {forecast['confidence']})"
        )
    if not any_forecast:
        lines.append("- ШІ-прогнозів по цих показниках ще немає")
    lines.append("")

    lines.append("3. КРИВА ДОХІДНОСТЕЙ (що ринок уже заклав):")
    try:
        # Той самий блок, що вже йде в промпт прогнозу показників
        # (`forecast_metric.py`) — один формат контексту на весь агент,
        # щоб "спред 2s10s" означало те саме в обох місцях.
        short = _yield_series(conn, *FORECAST_YIELD_SHORT)
        long = _yield_series(conn, *FORECAST_YIELD_LONG)
        yields = build_snapshot(short, long, date.today())
        snapshot = format_snapshot(yields)
        lines.append(snapshot or "- даних немає")
        for name, value in (("2y", yields.short_value), ("10y", yields.long_value)):
            if value is not None:
                anchors[name] = Anchor(float(value), CHANGE_GATE_YIELD_PP, relative=False)
    except Exception:
        logger.warning("Контекст дохідностей недоступний", exc_info=True)
        lines.append("- даних немає")
    lines.append("")

    news = fetch_market_news(conn)
    # Стан ринку — лише напрям: текст сесійного синтезу переписується
    # тричі на добу іншими словами, і з ним гейт не закривався б ніколи.
    stable["market"] = news["market"]["direction"] if news["market"] else None
    stable["stories"] = sorted(story["summary"] for story in news["stories"])
    lines.append("4. РИНКОВИЙ КОНТЕКСТ:")
    if news["market"]:
        lines.append(
            f"- стан ринку: {news['market']['direction']} "
            f"(упевненість {news['market']['confidence']}) — {news['market']['summary']}"
        )
    for story in news["stories"]:
        lines.append(f"- [{story['source_count']} джерел] {story['summary']}")
    if not news["market"] and not news["stories"]:
        lines.append("- підтверджених історій за вікно немає")
    lines.append("")

    speeches = fetch_recent_speeches(
        conn, bank, POLICY_SPEECH_DAYS_BACK, POLICY_SPEECH_DAYS_AHEAD,
    )
    lines.append("5. ВИСТУПИ ЧЛЕНІВ КОМІТЕТУ:")
    stable["speeches"] = []
    if speeches:
        for speech in speeches:
            when = "уже відбувся" if speech["scheduled_at"].date() <= date.today() else "запланований"
            stable["speeches"].append([str(speech["scheduled_at"]), speech["title"], when])
            lines.append(
                f"- {speech['scheduled_at']:%Y-%m-%d %H:%M} ({when}, "
                f"вплив {speech['impact_level'] or 'н/д'}): {speech['title']}"
            )
    else:
        lines.append("- у вікні немає")
    lines.append("")
    lines.append(
        "Зваж усі п'ять входів і спрогнозуй РІШЕННЯ найближчого засідання."
    )
    if gate is not None:
        gate["stable"], gate["anchors"] = stable, anchors
    return "\n".join(lines)


def parse_response(raw: str) -> dict:
    data = parse_json_object(raw, error_class=ValueError)

    decision = str(data.get("decision", "")).lower()
    if decision not in DECISIONS:
        raise ValueError(f"Неочікуване decision: {data.get('decision')!r}")
    if "summary" not in data:
        raise ValueError(f"У відповіді немає 'summary': {data!r}")

    expected_rate = data.get("expected_rate")
    if expected_rate is not None:
        try:
            expected_rate = float(expected_rate)
        except (TypeError, ValueError):
            raise ValueError(f"expected_rate не число: {expected_rate!r}")

    return {
        "decision": decision,
        "expected_rate": expected_rate,
        "confidence": parse_confidence(data.get("confidence", 0.5), error_class=ValueError),
        "summary": str(data["summary"]),
        "reasoning": str(data.get("reasoning") or data["summary"]),
        "watch_on_release": str(data.get("watch_on_release") or ""),
    }


def save(conn, bank: str, metric_id: str, meeting_at, result: dict, llm_call_id) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO policy_rate_synthesis
                (bank, metric_id, meeting_at, decision, expected_rate, confidence,
                 summary, reasoning, watch_on_release, llm_call_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (bank, meeting_at) DO UPDATE SET
                decision = EXCLUDED.decision,
                expected_rate = EXCLUDED.expected_rate,
                confidence = EXCLUDED.confidence,
                summary = EXCLUDED.summary,
                reasoning = EXCLUDED.reasoning,
                watch_on_release = EXCLUDED.watch_on_release,
                llm_call_id = EXCLUDED.llm_call_id,
                notified_at = NULL,
                created_at = now()
            """,
            (
                bank, metric_id, meeting_at, result["decision"], result["expected_rate"],
                result["confidence"], result["summary"], result["reasoning"],
                result["watch_on_release"], llm_call_id,
            ),
        )
    conn.commit()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", choices=sorted(POLICY_RATE_BANKS), default=None)
    parser.add_argument(
        "--horizon-days", type=int, default=POLICY_SYNTHESIS_HORIZON_DAYS,
        help="синтезувати лише банки, чиє засідання в межах цих днів",
    )
    parser.add_argument("--dry-run", action="store_true", help="показати промпт і вийти")
    parser.add_argument(
        "--force", action="store_true",
        help="синтезувати навіть без змін входів (оминути гейт змін)",
    )
    args = parser.parse_args()

    if not args.dry_run:
        require_api_key(PURPOSE)

    banks = [args.bank] if args.bank else sorted(POLICY_RATE_BANKS)
    conn = get_connection()
    try:
        done = 0
        for bank in banks:
            entry = POLICY_RATE_BANKS[bank]
            meeting = fetch_next_meeting(conn, entry["rate_metric"])
            if meeting is None:
                logger.info("%s: засідання не заведене в календар — пропущено", bank)
                continue
            days_to_meeting = (meeting.date() - date.today()).days
            if days_to_meeting > args.horizon_days:
                logger.info(
                    "%s: засідання через %d днів (горизонт %d) — ще рано",
                    bank, days_to_meeting, args.horizon_days,
                )
                continue

            gate: dict = {}
            prompt = build_prompt(conn, bank, entry, gate=gate)
            source_ref = f"{bank}:{meeting:%Y-%m-%d}"
            decision = change_gate.check(conn, PURPOSE, source_ref, gate["stable"], gate["anchors"])
            if args.dry_run:
                print(f"\n===== {bank} — гейт: {decision.reason} =====\n{prompt}")
                continue
            if not decision.changed and not args.force:
                logger.info(
                    "%s: %s (з %s) — синтез лишається чинним", bank, decision.reason, decision.since,
                )
                continue
            logger.info("%s: %s — синтезуємо", bank, decision.reason)

            try:
                llm = call_llm(prompt, SYSTEM_PROMPT, purpose=PURPOSE)
                result = parse_response(llm.text)
            except (LlmError, ValueError):
                logger.exception("%s: синтез ставки провалився — пропущено", bank)
                continue

            llm_call_id = log_llm_call(
                conn, llm=llm, purpose=PURPOSE, prompt=prompt, source_ref=source_ref,
            )
            save(conn, bank, entry["rate_metric"], meeting, result, llm_call_id)
            change_gate.remember(conn, PURPOSE, source_ref, gate["stable"], gate["anchors"])
            done += 1
            logger.info(
                "%s: засідання %s → %s (ставка %s, упевненість %.2f): %s",
                bank, meeting.date(), result["decision"].upper(),
                result["expected_rate"], result["confidence"], result["summary"][:120],
            )

        logger.info("Готово: синтезів ставки %d", done)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
