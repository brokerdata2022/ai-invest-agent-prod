"""
LLM-фільтрація релевантності новин через DeepSeek.

Межа шарів (rule 1, CLAUDE.md): це місце, де вирішується, чи новина
важлива і що вона означає — data-ingestion/news/ тексту не інтерпретує,
тільки збирає (докладніше docs/decisions.md, 2026-09-25).

Формат виходу — analysis/CLAUDE.md "Формат виходу LLM-аналізу":
summary/direction/confidence/reasoning обов'язкові. source_refs тут —
сама стаття (raw_news_id одразу відомий викликачу з БД), тому DeepSeek
його не генерує — ризик галюцинації для того, що й так відоме.

build_prompt()/parse_response() — чисті функції, тестуються на
фейкових прикладах без мережі. analyze_article() — наскрізний виклик,
що ходить у DeepSeek (тестується окремо/мокається).
"""

import logging
from dataclasses import dataclass
from typing import Optional

from llm_common import LlmResponse, DIRECTIONS, parse_confidence, parse_json_object
from llm_common import call_llm

logger = logging.getLogger(__name__)

# DIRECTIONS — спільний словник напрямків із llm_common.py (один
# контракт виходу на весь analysis/, analysis/CLAUDE.md). Набір полів
# тут ШИРШИЙ за синтезний: класифікація однієї статті додає
# is_relevant (і опційний asset_id).
_REQUIRED_FIELDS = ("is_relevant", "direction", "confidence", "summary", "reasoning")

SYSTEM_PROMPT = (
    "Ти фінансовий новинний аналітик. Тобі дають одну новинну статтю "
    "(заголовок, дата публікації, потік watchlist/general/geopolitical, "
    "опційно опис/уривок і список уже відстежуваних активів). "
    "\n\n"
    "is_relevant=true ТІЛЬКИ якщо стаття дає РЕАЛЬНУ, конкретну "
    "інформацію хоча б одного типу: (1) економічний показник/тренд із "
    "конкретними цифрами чи напрямком (інфляція, ставки, ВВП, "
    "зайнятість, звітність компанії з цифрами), (2) геополітична "
    "подія з прямим впливом на ринки/торгівлю/постачання (санкції, "
    "тарифи, конфлікт, вибори, що міняють економічний курс), (3) "
    "конкретний вплив на ціну/тренд активу (злиття, IPO з цифрами, "
    "зміна кредитного рейтингу, суттєвий рух ціни з причиною). "
    "\n\n"
    "is_relevant=false для: рутинних регуляторних дозволів/погоджень "
    "без ринкового наслідку, загальних оглядових статей без нових "
    "фактів, колонок думок без нової інформації, локальних новин без "
    "міжнародного економічного виміру, PR-анонсів без фінансової "
    "суті, статей, де заголовок звучить 'по темі', але текст не додає "
    "нічого конкретного понад це. Якщо сумніваєшся — false, не true: "
    "мета — сигнал, а не шум."
    "\n\n"
    "НІКОЛИ не давай прямих торгових рекомендацій ('купити'/'продати'/"
    "'входити в позицію') — тільки описові характеристики. Відповідай "
    "ЛИШЕ JSON-об'єктом з полями: is_relevant (bool, критерій вище), "
    "asset_id (string або null — тикер/ідентифікатор активу зі списку "
    "відстежуваних, якщо стаття прямо про нього, інакше null), "
    "direction (одне з: up, down, neutral, unclear), confidence "
    "(число від 0 до 1), summary (1-2 речення, тільки конкретні "
    "факти зі статті, не переказ заголовка), reasoning (коротке "
    "обґрунтування, чому саме ця категорія релевантності)."
)


@dataclass
class NewsAnalysisResult:
    is_relevant: bool
    asset_id: Optional[str]
    direction: str
    confidence: float
    summary: str
    reasoning: str


class DeepSeekResponseError(ValueError):
    pass


def build_prompt(article: dict, stream: str, tracked_assets: Optional[list[str]] = None) -> str:
    lines = [
        f"Потік: {stream}",
        f"Заголовок: {article['title']}",
        f"Дата публікації: {article['published_at']}",
        f"URL: {article['url']}",
    ]

    # Опис/уривок — коли є (RSS деякі фіди дають, GDELT не дає взагалі)
    # — без нього DeepSeek оцінює релевантність з самого заголовка,
    # що системно занадто шумно (docs/decisions.md, 2026-09-26): багато
    # заголовків "звучать фінансово", не несучи конкретного сигналу.
    description = extract_description(article)
    if description:
        lines.append(f"Опис: {description}")

    if tracked_assets:
        lines.append(f"Відстежувані активи: {', '.join(tracked_assets)}")
    return "\n".join(lines)


def extract_description(article: dict) -> Optional[str]:
    """raw_payload має різну форму per адаптер (RSS: {"description": ...},
    GDELT: сира відповідь GDELT без опису взагалі) — тому обережний
    .get(), не жорстка схема. Публічна (без "_") — спільна утиліта,
    consolidate.py теж її використовує (той самий формат raw_payload)."""
    raw_payload = article.get("raw_payload")
    if not isinstance(raw_payload, dict):
        return None
    description = raw_payload.get("description")
    return description.strip() if isinstance(description, str) and description.strip() else None


def parse_response(raw_content: str) -> NewsAnalysisResult:
    data = parse_json_object(raw_content, DeepSeekResponseError)

    missing = [f for f in _REQUIRED_FIELDS if f not in data]
    if missing:
        raise DeepSeekResponseError(f"У відповіді DeepSeek бракує полів {missing}: {data!r}")

    direction = data["direction"]
    if direction not in DIRECTIONS:
        raise DeepSeekResponseError(f"Неочікуване значення direction: {direction!r}")

    return NewsAnalysisResult(
        is_relevant=bool(data["is_relevant"]),
        asset_id=data.get("asset_id") or None,
        direction=direction,
        confidence=parse_confidence(data["confidence"], DeepSeekResponseError),
        summary=data["summary"],
        reasoning=data["reasoning"],
    )


def analyze_article(
    article: dict,
    stream: str,
    tracked_assets: Optional[list[str]] = None,
) -> tuple[NewsAnalysisResult, str, LlmResponse]:
    """Будує промпт → LLM → парсить. Повертає (результат, промпт,
    відповідь_LLM) — прompt/сира відповідь потрібні викликаючому коду
    для обов'язкового логування в llm_call_log (rule 5); ця функція без
    побічних ефектів у БД, як і адаптери в data-ingestion."""
    prompt = build_prompt(article, stream, tracked_assets)
    llm = call_llm(prompt, SYSTEM_PROMPT, purpose="news_relevance_filter")
    result = parse_response(llm.text)
    return result, prompt, llm
