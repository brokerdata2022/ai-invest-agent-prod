"""
LLM-синтез короткого висновку про вплив запланованих релізів на ринок —
"ранкове повідомлення" (рішення користувача, docs/decisions.md
2026-10-03): у понеділок на весь тиждень, у кожен інший робочий день на
поточний день.

Межа шарів (rule 1, CLAUDE.md; analysis/CLAUDE.md "детермінований vs
LLM"): самі дані релізу (час/impact_level/ринковий прогноз) уже лежать
у release_log — тут LLM лише ІНТЕРПРЕТУЄ готовий перелік, нічого не
рахує.

Формат виходу — стандартний SynthesisResult (analysis/CLAUDE.md
"Формат виходу LLM-аналізу": direction/confidence/summary/reasoning),
той самий контракт, що news_analysis/synthesize.py — нового власного
класу тут не потрібно, на відміну від expectations/synthesize.py
(impacts), бо вихід тут нічим не відрізняється від стандартного.
"""

from llm_common import LlmResponse, SynthesisResult, call_llm, parse_synthesis_response

SYSTEM_PROMPT = (
    "Ти макро-аналітик. Тобі дають перелік запланованих макроекономічних "
    "релізів (ще не вийшли) на найближчий період — показник, джерело, "
    "запланований час, рівень важливості (high/medium/low) і, якщо "
    "відомо, ринковий прогноз (forecast)."
    "\n\n"
    "Твоя задача — коротко написати, на що варто звернути увагу і чому: "
    "які саме релізи здатні рухати ринок (зазвичай high/medium), що "
    "вони означають (інфляція/зайнятість/ставки/зростання), і чи "
    "сукупно цей перелік схиляє очікування ринку в якийсь бік, чи ні."
    "\n\n"
    "Якщо релізів немає зовсім, або всі вони low-важливості — чесно "
    "скажи, що суттєвих подій не очікується, НЕ вигадуй штучну "
    "значущість."
    "\n\n"
    "НІКОЛИ не давай прямих торгових рекомендацій ('купити'/'продати'/"
    "'входити в позицію') — тільки описові характеристики. Відповідай "
    "ЛИШЕ JSON-об'єктом з полями: direction (одне з: up, down, neutral, "
    "unclear — сукупний очікуваний нахил ринкового сигналу від усього "
    "переліку разом, 'neutral' якщо релізів немає або вони несуттєві, "
    "'unclear' якщо різні релізи тягнуть у різні боки), confidence "
    "(число від 0 до 1), summary (2-4 речення — на що звернути увагу і "
    "чому), reasoning (коротке обґрунтування для аудиту)."
)


def build_prompt(entries: list[dict], scope: str) -> str:
    """entries — рядки release_log (fetch_upcoming), scope — 'week' або
    'day', лише для формулювання заголовка промпту."""
    period = "цей тиждень" if scope == "week" else "сьогодні"
    if not entries:
        return f"Запланованих релізів на {period} немає."

    lines = [f"Заплановані релізи на {period}:"]
    for entry in entries:
        impact = entry.get("impact_level") or "невідомо"
        forecast = entry.get("expected_value")
        line = (
            f"- {entry['metric_id']} ({entry['source']}), {entry['scheduled_at']}, "
            f"важливість: {impact}"
        )
        if forecast:
            line += f", ринковий прогноз: {forecast}"
        lines.append(line)
    return "\n".join(lines)


def synthesize_outlook(entries: list[dict], scope: str) -> tuple[SynthesisResult, str, LlmResponse]:
    """Будує промпт → LLM → парсить. Повертає (результат, промпт,
    відповідь_LLM) — обов'язкові для llm_common.log_llm_call (rule 5)."""
    prompt = build_prompt(entries, scope)
    llm = call_llm(prompt, SYSTEM_PROMPT, purpose="calendar_outlook")
    result = parse_synthesis_response(llm.text)
    return result, prompt, llm
