#!/usr/bin/env python3
"""
Семантичне об'єднання ВЖЕ КОНСОЛІДОВАНИХ записів (`news_consolidated`),
що описують ту саму подію іншими словами/іншим перекладом — окремий
крок ПІСЛЯ consolidate.py, не заміна йому (2026-09-29, живий приклад
користувача: 3 незалежні джерела дали "золото впало до 7-тижневого
мінімуму" трьома ОКРЕМИМИ записами в одному дайджесті).

## Чому text-similarity (SequenceMatcher/Jaccard) тут НЕ працює

Перевірено на реальних трьох summary про золото — SequenceMatcher-ratio
0.17-0.63 (набагато нижче будь-якого розумного порогу), Jaccard за
словами — так само низько: "семитижневого" і "7-тижневого" — РІЗНІ
токени попри однаковий сенс, кожен переклад формулює факт по-своєму.
Тут справді потрібне розуміння тексту, не збіг символів/слів.

## Чому не в самому consolidate.py (SYSTEM_PROMPT п.2 "ДОМЕРЖИТИ")

Інструкція вже була — LLM її не виконав надійно: 60 кластерів в одному
промпті означає, що модель має порівняти ~1770 пар (C(60,2)), а
фільтрація релевантності — вже основне навантаження того виклику.
Тут — ОКРЕМИЙ, вузький виклик лише на вже відфільтрованих (relevant)
записах одного вікна (типово одиниці-десятки, не 60) — набагато
легша задача для LLM.

Використання:
    python merge_similar.py --stream watchlist
    python merge_similar.py             # усі 3 потоки
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
from llm_common import LlmError, call_llm, log_llm_call, parse_json_object, require_api_key, resolve_provider  # noqa: E402
from news_analysis._consolidated_db import fetch_mergeable, merge_consolidated_rows  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

STREAMS = ("watchlist", "general", "geopolitical")

SYSTEM_PROMPT = (
    "Тобі дають ПРОНУМЕРОВАНИЙ список уже відфільтрованих ринкових "
    "новин (кожна — 1-2 речення, можливо різними словами про ту саму "
    "подію — незалежні джерела/переклади часто формулюють один факт "
    "по-різному: різні числа округлення, синоніми, порядок слів)."
    "\n\n"
    "Знайди записи, що описують ОДНУ Й ТУ САМУ подію (той самий "
    "актив, той самий рух ціни/факт, близький час) — навіть якщо "
    "формулювання зовсім різне. НЕ об'єднуй записи про РІЗНІ факти "
    "того самого активу (напр. окрема новина про причину ≠ новина "
    "про сам рух ціни, якщо це різні деталі, а не переказ того самого)."
    "\n\n"
    "Відповідай ЛИШЕ JSON-об'єктом з полем 'groups' — списком списків "
    "номерів: кожен внутрішній список — номери записів, що описують "
    "ТУ САМУ подію (мінімум 2 номери; записи без пари НЕ включай "
    "взагалі — не треба групи з одного номера)."
)


class MergeResponseError(ValueError):
    pass


def build_prompt(rows: list[dict]) -> str:
    lines = ["Записи:"]
    for i, row in enumerate(rows, start=1):
        asset = f"[{row['asset_id']}] " if row["asset_id"] else ""
        lines.append(f"{i}. {asset}{row['summary']}")
    return "\n".join(lines)


def parse_response(raw_content: str, n_rows: int) -> list[list[int]]:
    """Валідні номери — обов'язково (1..n_rows), інакше винятк — LLM
    вигадав неіснуючий номер, довіряти решті відповіді небезпечно.

    Перетин між групами (той самий номер у двох групах) — НЕ виняток:
    живо трапляється (2026-09-29, DeepSeek іноді дублює номер, коли
    запис неоднозначно схожий на кілька інших). Відкидати ВСЮ відповідь
    через один спірний номер шкідливіше, ніж просто лишити перше
    входження й прибрати з решти груп — той самий принцип, що
    "several LLM output": не давати одній хибі скасовувати решту
    правильної роботи."""
    data = parse_json_object(raw_content, MergeResponseError)

    groups = data.get("groups")
    if groups is None:
        raise MergeResponseError(f"У відповіді LLM бракує поля 'groups': {data!r}")
    if not isinstance(groups, list):
        raise MergeResponseError(f"'groups' має бути списком: {groups!r}")

    for group in groups:
        if not isinstance(group, list):
            raise MergeResponseError(f"Кожна група має бути списком номерів: {group!r}")
        for idx in group:
            if not isinstance(idx, int) or not (1 <= idx <= n_rows):
                raise MergeResponseError(f"Недійсний номер {idx!r} (маємо 1..{n_rows})")

    seen: set[int] = set()
    resolved: list[list[int]] = []
    for group in groups:
        deduped = [idx for idx in group if idx not in seen]
        seen.update(deduped)
        if len(deduped) >= 2:
            resolved.append(deduped)
        else:
            # Або LLM сам прислав групу з <2 (порушив системний промпт),
            # або перетин з попередньою групою звів її до <2 — обидва
            # випадки просто пропускаємо з попередженням, не валимо
            # решту відповіді через один спірний запис.
            logger.warning("Групу з <2 записів пропущено: %r (після дедупу: %r)", group, deduped)

    return resolved


def merge_stream(conn, stream: str, window_hours: int = 24) -> int:
    """Повертає кількість ЗЛИТИХ (видалених дублікатних) записів."""
    rows = fetch_mergeable(conn, stream, window_hours=window_hours)
    if len(rows) < 2:
        logger.info("%s: замало записів для перевірки на дублі (%d)", stream, len(rows))
        return 0

    prompt = build_prompt(rows)
    llm = call_llm(prompt, SYSTEM_PROMPT, purpose="news_merge_duplicates")
    raw_content = llm.text
    groups = parse_response(raw_content, len(rows))

    log_llm_call(
        conn, llm=llm, purpose="news_merge_duplicates",
        prompt=prompt, source_ref=stream,
    )

    if not groups:
        logger.info("%s: LLM не знайшов дублікатів серед %d записів", stream, len(rows))
        return 0

    merged_count = 0
    for group in groups:
        group_rows = [rows[i - 1] for i in group]
        removed = merge_consolidated_rows(conn, group_rows)
        merged_count += removed
        logger.info(
            "%s: об'єднано %d записів в один (%s)",
            stream, len(group_rows), group_rows[0]["summary"][:60],
        )
    return merged_count


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", choices=STREAMS, default=None)
    parser.add_argument("--window-hours", type=int, default=24)
    args = parser.parse_args()

    require_api_key("news_merge_duplicates")
    streams = [args.stream] if args.stream else list(STREAMS)

    conn = get_connection()
    try:
        total = 0
        for stream in streams:
            try:
                total += merge_stream(conn, stream, window_hours=args.window_hours)
            except MergeResponseError:
                logger.exception("%s: некоректна відповідь LLM — пропущено", stream)
            except LlmError:
                logger.exception("%s: збій виклику LLM — пропущено", stream)
        logger.info("Готово: %d дублікатів об'єднано загалом", total)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
