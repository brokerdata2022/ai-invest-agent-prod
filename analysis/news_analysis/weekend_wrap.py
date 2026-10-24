#!/usr/bin/env python3
"""
ПІДСУМОК ВИХІДНИХ — неділя ввечері, перед відкриттям Азії (2026-10-24,
рішення користувача разом із вимкненням аналізів на закритих ринках).

**Навіщо.** Денні брифи, синтези по акціях/товарах/валютах і список
для торгів у вихідні більше не робляться (`common/market_hours.py`):
ринки стоять, і п'ятничні висновки повторювались би. Але за вихідні
відбувається те, що дає ГЕП у понеділок: геополітика, заяви
центробанків, рух крипти (вона торгується 24/7 і першою показує
ризик-апетит). Один виклик Anthropic замість ~16, що йшли за вихідні
до зміни.

Вхід — те, що вже пораховано іншими кроками, без нового збору:
підтверджені історії з `news_consolidated` (2+ джерела) за вікно від
закриття п'ятниці, рух крипти зі списку обраних за той самий час і
макро-контекст (дохідності/долар на закриття п'ятниці).

Використання:
    python weekend_wrap.py --dry-run
    python weekend_wrap.py
"""

import argparse
import json
import logging
import os
import sys
from datetime import date

from dotenv import load_dotenv

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_ANALYSIS_DIR)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _ANALYSIS_DIR)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))

from common.db import get_connection  # noqa: E402
from common.watchlist_db import fetch_price_sources, fetch_watchlist  # noqa: E402
from config import (  # noqa: E402
    MARKET_SYNTHESIS_MIN_SOURCES,
    WEEKEND_WRAP_HOURS,
    WEEKEND_WRAP_MAX_STORIES,
)
from llm_common import (  # noqa: E402
    DIRECTIONS,
    LlmError,
    SynthesisResponseError,
    call_llm,
    log_llm_call,
    parse_confidence,
    parse_json_object,
    require_api_key,
)
from news_analysis._db import fetch_consolidated_as_clusters  # noqa: E402
from news_analysis.prices import fetch_price_change  # noqa: E402
from news_analysis.synthesize_market import fetch_macro_context  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PURPOSE = "weekend_wrap"
STREAMS = ("geopolitical", "general", "watchlist")
MAX_POINTS = 5

SYSTEM_PROMPT = (
    "Ти макроаналітик, що готує короткий підсумок ВИХІДНИХ перед "
    "відкриттям ринків у понеділок. Біржі акцій, форекс і товари були "
    "закриті з п'ятниці; крипта торгувалась увесь час. Тобі дають "
    "підтверджені кількома джерелами історії за вихідні, рух крипти за "
    "той самий час і макро-контекст на закриття п'ятниці.\n\n"
    "Твоя задача — сказати, що з цього може дати ГЕП на відкритті: які "
    "події змінюють картину відносно п'ятниці і для яких класів активів "
    "(акції, валюти, нафта, золото). Рух крипти — ранній індикатор "
    "ризик-апетиту: скажи, що він показує. Якщо суттєвого нічого не "
    "сталось — так і скажи, це теж висновок.\n\n"
    "Спирайся ЛИШЕ на надані дані. НІКОЛИ не давай торгових "
    "рекомендацій і не називай цільових цін. Відповідай ЛИШЕ "
    "JSON-обʼєктом з полями: summary (2-3 речення: головне за вихідні), "
    "direction (одне з: up, down, neutral, unclear — куди тягне "
    "ризик-апетит на відкритті), confidence (0..1), gap_risks (МАСИВ "
    f"рядків — клас активу чи актив і чому можливий геп, максимум {MAX_POINTS}; "
    "порожній, якщо підстав немає), crypto (1-2 речення: що показала "
    "крипта за вихідні), watch_monday (МАСИВ рядків — на що дивитись "
    f"першим у понеділок, максимум {MAX_POINTS})."
)


def _points(raw) -> list[str]:
    return [str(x).strip() for x in (raw or []) if str(x).strip()][:MAX_POINTS]


def parse_response(raw: str) -> dict:
    data = parse_json_object(raw, error_class=SynthesisResponseError)
    if not str(data.get("summary") or "").strip():
        raise SynthesisResponseError(f"Немає summary: {data!r}")
    direction = data.get("direction")
    if direction not in DIRECTIONS:
        raise SynthesisResponseError(f"Неочікуване значення direction: {direction!r}")
    return {
        "summary": str(data["summary"]).strip(),
        "direction": direction,
        "confidence": parse_confidence(data.get("confidence", 0.5)),
        "gap_risks": _points(data.get("gap_risks")),
        "crypto": str(data.get("crypto") or "").strip(),
        "watch_monday": _points(data.get("watch_monday")),
    }


def build_prompt(stories: list[dict], crypto: list[dict], macro: dict) -> str:
    lines = ["Макро-контекст на закриття п'ятниці:"]
    if macro:
        for label, entry in macro.items():
            lines.append(f"- {label}: {entry['latest_value']} ({entry['latest_date']})")
    else:
        lines.append("- немає даних")

    lines.append("")
    lines.append(f"Рух крипти за вихідні ({len(crypto)}):")
    if crypto:
        for item in crypto:
            lines.append(
                f"- {item['label']}: {item['pct_change']:+.2f}% "
                f"({item['start_date']} → {item['end_date']})"
            )
    else:
        lines.append("- даних немає")

    lines.append("")
    lines.append(f"Підтверджені історії за вихідні ({len(stories)}):")
    if stories:
        for story in stories:
            asset = f", актив {story['asset_id']}" if story.get("asset_id") else ""
            lines.append(
                f"- [{story['source_count']} джерел, напрямок {story['direction']}{asset}] "
                f"{story['summary']}"
            )
    else:
        lines.append("- підтверджених історій немає")
    lines.append("")
    lines.append("Що з цього може дати геп на відкритті в понеділок?")
    return "\n".join(lines)


def fetch_crypto_moves(conn, days: int) -> list[dict]:
    price_sources = fetch_price_sources(conn)
    moves = []
    for asset in fetch_watchlist(conn):
        if asset.get("asset_class") != "crypto":
            continue
        change = fetch_price_change(conn, asset["asset_id"], days=days, price_sources=price_sources)
        if change is None:
            continue
        moves.append({
            "label": asset.get("label") or asset["asset_id"],
            "pct_change": float(change.pct_change),
            "start_date": change.start_date,
            "end_date": change.end_date,
        })
    return moves


def save(conn, wrap_date, result: dict, inputs: dict, llm_call_id) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO weekend_wrap
                (wrap_date, summary, direction, confidence, gap_risks, crypto,
                 watch_monday, inputs, llm_call_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (wrap_date) DO UPDATE SET
                summary = EXCLUDED.summary,
                direction = EXCLUDED.direction,
                confidence = EXCLUDED.confidence,
                gap_risks = EXCLUDED.gap_risks,
                crypto = EXCLUDED.crypto,
                watch_monday = EXCLUDED.watch_monday,
                inputs = EXCLUDED.inputs,
                llm_call_id = EXCLUDED.llm_call_id,
                created_at = now(),
                notified_at = NULL
            """,
            (
                wrap_date, result["summary"], result["direction"], result["confidence"],
                json.dumps(result["gap_risks"], ensure_ascii=False), result["crypto"],
                json.dumps(result["watch_monday"], ensure_ascii=False),
                json.dumps(inputs, ensure_ascii=False, default=str), llm_call_id,
            ),
        )
    conn.commit()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="надрукувати промпт без виклику ШІ")
    parser.add_argument("--hours", type=int, default=WEEKEND_WRAP_HOURS)
    args = parser.parse_args()

    if not args.dry_run:
        require_api_key(PURPOSE)

    conn = get_connection()
    try:
        stories = fetch_consolidated_as_clusters(
            conn, STREAMS, args.hours, MARKET_SYNTHESIS_MIN_SOURCES, WEEKEND_WRAP_MAX_STORIES,
        )
        # Вікно в днях для ціни: від закриття п'ятниці (60 год) з
        # округленням угору — щоб у вибірку потрапила п'ятнична точка.
        crypto = fetch_crypto_moves(conn, days=max(1, -(-args.hours // 24)))
        macro = fetch_macro_context(conn)
        logger.info(
            "Підсумок вихідних: історій %d, крипто-активів %d, макро-показників %d",
            len(stories), len(crypto), len(macro),
        )
        if not stories and not crypto:
            logger.info("За вихідні немає ні історій, ні руху крипти — підсумок пропущено")
            return

        prompt = build_prompt(stories, crypto, macro)
        if args.dry_run:
            print(prompt)
            logger.info("--dry-run: ШІ не викликався, у БД не записано")
            return

        try:
            llm = call_llm(prompt, SYSTEM_PROMPT, purpose=PURPOSE)
        except LlmError:
            logger.exception("Збій виклику LLM — підсумок пропущено")
            return
        llm_call_id = log_llm_call(conn, llm=llm, purpose=PURPOSE, prompt=prompt, source_ref=str(date.today()))
        try:
            result = parse_response(llm.text)
        except SynthesisResponseError:
            logger.exception("Некоректна відповідь LLM — підсумок пропущено")
            return
        save(
            conn, date.today(), result,
            {"stories": len(stories), "crypto": crypto, "hours": args.hours},
            llm_call_id,
        )
        logger.info("Підсумок вихідних: %s (%s)", result["summary"][:160], result["direction"])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
