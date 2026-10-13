#!/usr/bin/env python3
"""
LLM-синтез причинного висновку "факт vs очікування" (docs/decisions.md,
"наступний крок Фази 2"): бере вже готовий детермінований сюрприз
(compare_releases.py:expectation_comparisons) і формує людський
висновок "вийшло X, очікувалось Y, це означає Z" — критерій завершення
Фази 2 (docs/archive/plan-journal-2026-10-11.md).

Межа шарів (rule 1, CLAUDE.md; analysis/CLAUDE.md "детермінований vs
LLM"): сам сюрприз (surprise/surprise_pct) уже порахований звичайним
кодом у compare_releases.py — тут LLM лише ІНТЕРПРЕТУЄ готове число, не
рахує його заново.

Формат виходу — analysis/CLAUDE.md "Формат виходу LLM-аналізу":
summary/direction/confidence/reasoning від LLM; source_refs (сам
показник/метод порівняння, на якому базується висновок) приєднує код,
не LLM — той самий принцип, що news_analysis/synthesize.py. Додатково
(2026-10-02, живий фідбек користувача) — `impacts`: розбір впливу
релізу на ІНШІ категорії активів (ставка/економіка/валюта/акції/
крипта/золото/інший_актив), що НЕ вписується в стандартний
SynthesisResult (llm_common.py) — тому власний клас
ExpectationSynthesisResult і власний парсер тут
(parse_expectation_synthesis_response), а не
llm_common.parse_synthesis_response().

Межа шарів / формат виходу — той самий принцип, що
news_analysis/synthesize.py; провайдер (SYNTHESIS_LLM_PROVIDER, дефолт
"deepseek"), сам виклик і аудит-лог — спільний код `llm_common.py`
(call_llm/log_llm_call/parse_json_object/parse_confidence); тут
лишається своє: SYSTEM_PROMPT, build_prompt() і розбір розширеної
форми відповіді (impacts).

Використання (після analysis/expectations/compare_releases.py):
    python synthesize.py
    python synthesize.py --limit 5
"""

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field

from dotenv import load_dotenv
import requests

_ANALYSIS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ANALYSIS_DIR)
sys.path.insert(0, os.path.join(_ANALYSIS_DIR, "..", "data-ingestion"))

from common.db import get_connection  # noqa: E402
from llm_common import (  # noqa: E402
    LlmError,
    DIRECTIONS,
    SYNTHESIS_FIELDS,
    SynthesisResponseError,
    call_llm,
    log_llm_call,
    parse_confidence,
    parse_json_object,
    require_api_key,
)

from expectations._db import fetch_unsynthesized_comparisons, save_synthesis  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Категорії для розбору міжактивного впливу (impacts нижче) — той самий
# перелік, що просив користувач 2026-10-02: ставка/економіка/валюта/
# акції/крипта/золото + "інший_актив" для всього іншого (товари,
# конкретні тикери поза списком). Вільний текст, не enum у БД — LLM
# сам пише конкретику в "assets", категорія лише групує для читабельності.
IMPACT_CATEGORIES = (
    "ставка", "економіка", "валюта", "акції", "крипта", "золото", "інший_актив",
)

SYSTEM_PROMPT = (
    "Ти макро-аналітик. Тобі дають один макроекономічний реліз: яке "
    "фактичне значення вийшло, яке було ринкове очікування (прогноз), "
    "і вже порахований числовий сюрприз (факт мінус очікування, у тих "
    "самих приведених одиницях і, якщо доступно, у відсотках від "
    "очікування)."
    "\n\n"
    "Твоя задача — коротко пояснити, ЩО ЦЕ ОЗНАЧАЄ: наскільки суттєве "
    "це відхилення від очікувань і в який бік (гірше/краще з точки "
    "зору відповідного тренду — інфляція/зайнятість/зростання/ставки), "
    "і який напрямок ринкового сигналу для пов'язаної валюти/активу це "
    "імовірно означає."
    "\n\n"
    "Додатково — розпиши ПОВНИЙ вплив цього релізу на ІНШІ категорії "
    "активів, не тільки на пов'язану валюту: "
    f"{', '.join(IMPACT_CATEGORIES)}. Для кожної категорії подумай, чи "
    "цей конкретний реліз логічно й змістовно на неї впливає (напр. "
    "сюрприз по інфляції/зайнятості змінює очікування щодо ставки "
    "ФРС/ЄЦБ → це впливає на ризикові активи (акції, крипта) і на "
    "захисні (золото), а не лише на валюту випуску)."
    "\n\n"
    "КРИТИЧНО: включай у impacts ТІЛЬКИ категорії з РЕАЛЬНИМ і "
    "змістовним впливом. Якщо на якусь категорію реліз не впливає або "
    "вплив незначний — НЕ згадуй її в impacts взагалі (не пиши "
    "'не впливає'/'нейтрально без причини' — просто пропусти). Якщо "
    "впливає — опиши конкретно: які саме активи/валюти/сектори і чому."
    "\n\n"
    "НІКОЛИ не давай прямих торгових рекомендацій ('купити'/'продати'/"
    "'входити в позицію') — тільки описові характеристики. Відповідай "
    "ЛИШЕ JSON-об'єктом з полями: direction (одне з: up, down, neutral, "
    "unclear — напрямок ринкового сигналу для основного пов'язаного "
    "активу/валюти, не просто знак сюрпризу), confidence (число від 0 "
    "до 1 — наскільки однозначний висновок), summary (1-2 речення у "
    "форматі 'вийшло X, очікувалось Y, це означає Z'), reasoning "
    "(коротке обґрунтування для аудиту), impacts (МАСИВ об'єктів, може "
    "бути порожнім — по одному на кожну РЕАЛЬНО зачеплену категорію; "
    "кожен об'єкт: category — одне з "
    f"{', '.join(IMPACT_CATEGORIES)}; assets — конкретні назви "
    "активів/валют/тикерів, яких стосується; direction — "
    "up/down/neutral/unclear; explanation — 1-2 речення чому саме так)."
)


@dataclass
class ExpectationSynthesisResult:
    """Розширений вихід синтезу факт/очікування — ІНША форма за
    llm_common.SynthesisResult (додає impacts), тому свій клас і свій
    парсер тут, а не parse_synthesis_response() (analysis/CLAUDE.md:
    "форма результату лишається в скрипті, коли вона інша")."""

    direction: str
    confidence: float
    summary: str
    reasoning: str
    impacts: list = field(default_factory=list)


def _parse_impacts(raw_impacts) -> list[dict]:
    """Валідує масив impacts з відповіді LLM. На відміну від основних
    полів (direction/confidence/summary/reasoning), тут НЕ валимо весь
    синтез через один некоректний елемент масиву — некоректний запис
    просто пропускається з логом, бо основний висновок (summary/
    direction) лишається корисним і без повного розбору impacts."""
    impacts = []
    for item in raw_impacts or []:
        if not isinstance(item, dict):
            logger.warning("impacts: елемент не є об'єктом, пропущено: %r", item)
            continue
        direction = item.get("direction")
        category = str(item.get("category") or "").strip()
        explanation = str(item.get("explanation") or "").strip()
        if direction not in DIRECTIONS or not category or not explanation:
            logger.warning("impacts: некоректний елемент, пропущено: %r", item)
            continue
        impacts.append({
            "category": category,
            "assets": str(item.get("assets") or "").strip(),
            "direction": direction,
            "explanation": explanation,
        })
    return impacts


def parse_expectation_synthesis_response(raw_content: str) -> ExpectationSynthesisResult:
    data = parse_json_object(raw_content, error_class=SynthesisResponseError)

    missing = [f for f in SYNTHESIS_FIELDS if f not in data]
    if missing:
        raise SynthesisResponseError(f"У відповіді LLM бракує полів {missing}: {data!r}")

    direction = data["direction"]
    if direction not in DIRECTIONS:
        raise SynthesisResponseError(f"Неочікуване значення direction: {direction!r}")

    return ExpectationSynthesisResult(
        direction=direction,
        confidence=parse_confidence(data["confidence"], error_class=SynthesisResponseError),
        summary=data["summary"],
        reasoning=data["reasoning"],
        impacts=_parse_impacts(data.get("impacts")),
    )


def build_prompt(comparison: dict) -> str:
    lines = [
        f"Показник: {comparison['metric_id']} ({comparison['source']})",
        f"Дата спостереження: {comparison['observed_at']}",
        f"Рівень впливу релізу: {comparison['impact_level']}",
        f"Метод приведення до спільних одиниць: {comparison['comparison_method']}",
        f"Факт: {float(comparison['actual_value']):.4g}",
        f"Очікування (прогноз, приведений до тих самих одиниць): "
        f"{float(comparison['expected_value_parsed']):.4g} "
        f"(оригінальний текст прогнозу: {comparison['expected_value_raw']})",
        f"Сюрприз (факт - очікування): {float(comparison['surprise']):+.4g}",
    ]
    if comparison.get("surprise_pct") is not None:
        lines.append(
            f"Сюрприз у % від очікування: {float(comparison['surprise_pct']):+.2f}%"
        )
    return "\n".join(lines)


def synthesize_comparison(comparison: dict):
    """Будує промпт → LLM → парсить. Повертає (результат, промпт,
    відповідь_LLM) — обов'язкові для логування в llm_call_log
    (rule 5), той самий контракт, що news_analysis/synthesize.py."""
    prompt = build_prompt(comparison)
    llm = call_llm(prompt, SYSTEM_PROMPT, purpose="expectation_synthesis")
    result = parse_expectation_synthesis_response(llm.text)
    return result, prompt, llm


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    require_api_key("expectation_synthesis")

    conn = get_connection()
    try:
        comparisons = fetch_unsynthesized_comparisons(conn, limit=args.limit)
        logger.info("%d порівнянь без синтезу", len(comparisons))

        synthesized = 0
        for comparison in comparisons:
            try:
                result, prompt, llm = synthesize_comparison(comparison)
            except SynthesisResponseError:
                logger.exception(
                    "%s: некоректна відповідь LLM — пропущено", comparison["metric_id"]
                )
                continue
            except LlmError:
                logger.exception(
                    "%s: збій виклику LLM — пропущено", comparison["metric_id"]
                )
                continue

            llm_call_id = log_llm_call(
                conn,
                llm=llm,
                purpose="expectation_synthesis",
                prompt=prompt,
                source_ref=str(comparison["comparison_id"]),
            )
            source_refs = [{
                "metric_id": comparison["metric_id"],
                "source": comparison["source"],
                "observed_at": str(comparison["observed_at"]),
                "comparison_method": comparison["comparison_method"],
            }]
            save_synthesis(
                conn,
                comparison_id=comparison["comparison_id"],
                result=result,
                source_refs=source_refs,
                llm_call_id=llm_call_id,
            )
            synthesized += 1
            logger.info(
                "%s → direction=%s confidence=%.2f: %s",
                comparison["metric_id"], result.direction, result.confidence, result.summary,
            )
    finally:
        conn.close()

    logger.info("Готово: %d порівнянь синтезовано", synthesized)


if __name__ == "__main__":
    main()
