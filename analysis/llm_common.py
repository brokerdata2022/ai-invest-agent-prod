"""
Спільна LLM-"проводка" для всіх аналітичних скриптів analysis/, що
викликають модель: вибір провайдера, сам виклик, розбір структурованої
відповіді, обов'язковий аудит-лог виклику (rule 5 кореневого CLAUDE.md).

## Чому цей модуль існує (зміна рішення, 2026-09-27)

Раніше `SynthesisResult`/`SynthesisResponseError`/`parse_response()`/
`call_llm()`/`log_llm_call()` були СВІДОМО дубльовані в кожному
LLM-скрипті ("кожен скрипт самодостатній", docs/decisions.md
2026-09-27). За чотирма скриптами (`news_analysis/synthesize.py`,
`news_analysis/synthesize_market.py`, `discovery/news_tickers.py`,
`expectations/synthesize.py`) це стало ~150 рядків байт-у-байт копій,
а головне — перехід на Anthropic (CLAUDE.md: "Anthropic API фінальний
висновок аналіз") вимагав би однакової правки в чотирьох місцях, де
легко пропустити одне. Тепер провайдер перемикається В ОДНОМУ місці —
`call_llm()` нижче.

Що НЕ винесено сюди (залишається в кожному скрипті, бо справді різне):
`SYSTEM_PROMPT`, `build_prompt()` і форма результату, коли вона інша
(`discovery/news_tickers.py` віддає список витягнутих тикерів, не
`SynthesisResult`; `relevance_filter.py` — `NewsAnalysisResult` з
`is_relevant`/`asset_id`).

## Провайдер — МАРШРУТ ЗА ПРИЗНАЧЕННЯМ, не одне глобальне значення

2026-10-05 (рішення користувача після заміру обсягів): провайдер і
модель обираються за `purpose` з `config.py:LLM_ROUTES` —
Opus 5 для прогнозу, Sonnet 5 для висновків, які читає людина,
DeepSeek для механіки (багато викликів, проста задача). Обґрунтування
й заміряні числа — у самому конфігу, розділ 9.

До цього був один глобальний `SYNTHESIS_LLM_PROVIDER`. Він лишається
як АВАРІЙНЕ перекриття всіх маршрутів одразу (напр. "перевести все на
DeepSeek, бо скінчився бюджет"), але більше не є нормальним способом
налаштування: завдання різні за природою, і платити за них однаково
немає сенсу.

`resolve_route()` читає env щоразу, не при імпорті модуля — інакше
значення "замерзало" на стані env під час імпорту (у тестах це
вимагало monkeypatch константи в кожному модулі окремо).

## Резерв провайдера

`call_llm()` на транспортному збої Anthropic (429/5xx/таймаут)
повторює виклик на DeepSeek і позначає це в аудит-лозі
(`llm_call_log.fallback_from`). Пряме застосування уроку 2026-10-05:
одноджерельність неприпустима, а "працює, але на дешевшій моделі" не
має виглядати як норма.
"""

import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Optional

import requests

import config
from anthropic_client import AnthropicError, call_anthropic, call_anthropic_batch
from news_analysis.deepseek_client import DeepSeekError, call_deepseek

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "deepseek"

# Спільний словник напрямків сигналу для ВСІХ LLM-виходів проєкту
# (analysis/CLAUDE.md "Формат виходу LLM-аналізу") — і синтезу тут, і
# класифікації окремої статті (`relevance_filter.py` імпортує звідси).
DIRECTIONS = frozenset({"up", "down", "neutral", "unclear"})

# Мінімум полів структурованого висновку синтезу (той самий контракт з
# analysis/CLAUDE.md; `source_refs` приєднує код, не LLM — ризик
# галюцинації того, що й так відоме викликачу).
SYNTHESIS_FIELDS = ("direction", "confidence", "summary", "reasoning")


@dataclass
class SynthesisResult:
    direction: str
    confidence: float
    summary: str
    reasoning: str


class SynthesisResponseError(ValueError):
    pass


@dataclass
class LlmRoute:
    """Куди йде конкретний виклик: провайдер, модель і параметри
    глибини. `effort`/`thinking`/`max_tokens`/`batch` застосовні лише
    до Anthropic (налаштування — `config.py`, розділ 9)."""

    provider: str
    model: str
    effort: Optional[str] = None
    max_tokens: int = 4096
    thinking: Optional[str] = None
    batch: bool = False


@dataclass
class LlmResponse:
    """Сирий текст + ФАКТИЧНО використаний провайдер/модель.

    Чому не просто рядок (як було до 2026-10-05): з маршрутизацією й
    резервом викликач більше не може знати наперед, хто відповів —
    а в аудит-лог (rule 5) мусить піти правда, не очікування."""

    text: str
    provider: str
    model: str
    fallback_from: Optional[str] = None


class LlmError(RuntimeError):
    """Спільний тип транспортного/протокольного збою LLM, незалежно від
    провайдера.

    Потрібен, бо провайдери падають по-різному: DeepSeek — через
    `requests.exceptions.RequestException`, Anthropic SDK — власною
    іє­рархією. Без спільного типу кожен з ~11 викликачів мусив би
    ловити обидві, і додавання третього провайдера знову зламало б
    усіх."""


def resolve_provider(purpose: Optional[str] = None) -> str:
    """Провайдер для призначення (без purpose — дефолтний маршрут).
    Лишається для сумісності й для логів; сам вибір — `resolve_route()`."""
    return resolve_route(purpose).provider


def resolve_route(purpose: Optional[str] = None) -> LlmRoute:
    """Маршрут для призначення: `config.LLM_ROUTES[purpose]`, інакше
    `LLM_ROUTE_DEFAULT`.

    `SYNTHESIS_LLM_PROVIDER` у .env перекриває ПРОВАЙДЕРА всіх
    маршрутів одразу — аварійний важіль (скінчився бюджет, провайдер
    лежить). Модель при перекриванні береться резервна для цього
    провайдера, бо модель Anthropic не має сенсу для DeepSeek і
    навпаки."""
    raw = config.LLM_ROUTES.get(purpose or "", config.LLM_ROUTE_DEFAULT)
    route = LlmRoute(
        provider=raw["provider"],
        model=raw["model"],
        effort=raw.get("effort"),
        max_tokens=raw.get("max_tokens", 4096),
        thinking=raw.get("thinking"),
        batch=bool(raw.get("batch")),
    )

    override = os.environ.get("SYNTHESIS_LLM_PROVIDER")
    if override and override != route.provider:
        logger.warning(
            "SYNTHESIS_LLM_PROVIDER=%r перекриває маршрут %s (було %s/%s)",
            override, purpose, route.provider, route.model,
        )
        route = LlmRoute(provider=override, model=_default_model_for(override))
    return route


def _default_model_for(provider: str) -> str:
    return {
        "deepseek": config.LLM_FALLBACK_MODEL,
        "anthropic": config.ANTHROPIC_MODEL_SYNTHESIS,
    }.get(provider, config.LLM_FALLBACK_MODEL)


def api_key_env_for(provider: str) -> Optional[str]:
    """Назва env-змінної з ключем для провайдера, або None якщо
    провайдер невідомий. Окремо від `require_api_key()`, щоб перелік
    провайдерів жив в одному місці."""
    return {
        "deepseek": "DEEPSEEK_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }.get(provider)


def require_api_key(purpose: Optional[str] = None, provider: Optional[str] = None) -> str:
    """Ключ провайдера ДЛЯ ЦЬОГО призначення або вихід із кодом 1 і
    зрозумілим повідомленням — той самий early-exit, що був
    скопійований у main() кожного LLM-скрипта.

    `provider` задається явно, коли маршрут перекривається на льоту
    (бектест із `--model`) — інакше перевірявся б ключ не того
    провайдера, і скрипт упав би вже посеред прогону.

    Повертається ключ лише для перевірки "він є"; сам `call_llm()`
    читає ключі сам, бо при резерві провайдер може змінитись."""
    route = resolve_route(purpose)
    provider = provider or route.provider
    env_name = api_key_env_for(provider)
    if env_name is None:
        logger.error(
            "Непідтримуваний провайдер %r (маршрут %s) — доступні: deepseek, anthropic.",
            provider, purpose,
        )
        sys.exit(1)

    api_key = os.environ.get(env_name)
    if not api_key:
        logger.error(
            "%s не задано, а маршрут %s веде на %s. Додайте ключ у .env "
            "(див. .env.example).",
            env_name, purpose, provider,
        )
        sys.exit(1)
    return api_key


def _call_provider(route: LlmRoute, prompt: str, system_prompt: str) -> str:
    env_name = api_key_env_for(route.provider)
    api_key = os.environ.get(env_name) if env_name else None
    if not api_key:
        raise LlmError(f"{env_name} не задано для провайдера {route.provider!r}")

    if route.provider == "deepseek":
        return call_deepseek(prompt, api_key=api_key, system_prompt=system_prompt)
    if route.provider == "anthropic":
        return call_anthropic(
            prompt,
            api_key=api_key,
            system_prompt=system_prompt,
            model=route.model,
            effort=route.effort,
            max_tokens=route.max_tokens,
            thinking=route.thinking,
        )
    raise LlmError(f"Непідтримуваний провайдер {route.provider!r}")


def call_llm(
    prompt: str,
    system_prompt: str,
    purpose: Optional[str] = None,
    route: Optional[LlmRoute] = None,
) -> LlmResponse:
    """ЄДИНЕ місце, де вибирається провайдер LLM для всього analysis/.

    `route` перекриває маршрут із конфіга для ОДНОГО виклику —
    потрібно бектесту, який за визначенням міряє різні моделі на тих
    самих даних (`backtest_llm.py --model`). Бойові скрипти його не
    передають: там маршрут має бути один і з конфіга.

    Кидає `LlmError` на будь-якому транспортному збої — незалежно від
    провайдера, щоб викликачі ловили один тип.

    Резерв: маршрут на Anthropic, що впав транспортно, повторюється на
    DeepSeek (`config.LLM_FALLBACK_PROVIDER`). Резерв НЕ застосовується
    до відмови класифікатора безпеки й до вже-дешевого маршруту —
    повторювати DeepSeek на DeepSeek безглуздо."""
    route = route or resolve_route(purpose)

    try:
        text = _call_provider(route, prompt, system_prompt)
        return LlmResponse(text=text, provider=route.provider, model=route.model)
    except (requests.exceptions.RequestException, DeepSeekError, AnthropicError) as primary_error:
        fallback = config.LLM_FALLBACK_PROVIDER
        if route.provider == fallback or not fallback:
            raise LlmError(f"{route.provider}/{route.model}: {primary_error}") from primary_error

        logger.warning(
            "%s/%s впав (%s) — повторюю на резервному %s",
            route.provider, route.model, primary_error, fallback,
        )
        fallback_route = LlmRoute(provider=fallback, model=config.LLM_FALLBACK_MODEL)
        try:
            text = _call_provider(fallback_route, prompt, system_prompt)
        except (requests.exceptions.RequestException, DeepSeekError, AnthropicError) as fallback_error:
            raise LlmError(
                f"обидва провайдери впали: {route.provider} ({primary_error}), "
                f"{fallback} ({fallback_error})"
            ) from fallback_error

        return LlmResponse(
            text=text,
            provider=fallback_route.provider,
            model=fallback_route.model,
            fallback_from=f"{route.provider}/{route.model}",
        )


def call_llm_batch(
    items: list[tuple[str, str]],
    system_prompt: str,
    purpose: Optional[str] = None,
) -> dict[str, object]:
    """Кілька НЕЗАЛЕЖНИХ промптів одного призначення: {ключ: LlmResponse
    або LlmError}.

    Якщо маршрут має `batch=True` і веде на Anthropic — один пакет
    через Message Batches API (−50% до ціни, 2026-10-24). Інакше, і
    також коли пакет не завершився вчасно чи впав цілком, — звичайні
    `call_llm()` по одному (з тим самим резервом на DeepSeek), тобто
    пакет ніколи не гірший за послідовні виклики, лише дешевший.

    Помилка одного промпту повертається як `LlmError` у словнику, а не
    кидається: решта пакета лишається корисною."""
    route = resolve_route(purpose)
    results: dict[str, object] = {}

    if route.provider == "anthropic" and route.batch and items:
        api_key = os.environ.get(api_key_env_for(route.provider) or "")
        try:
            raw = call_anthropic_batch(
                items,
                api_key=api_key,
                system_prompt=system_prompt,
                model=route.model,
                effort=route.effort,
                max_tokens=route.max_tokens,
                thinking=route.thinking,
                poll_seconds=config.ANTHROPIC_BATCH_POLL_SECONDS,
                max_wait_seconds=config.ANTHROPIC_BATCH_MAX_WAIT_SECONDS,
            )
        except Exception:
            logger.warning(
                "Batch для %s не вдався — повторюю звичайними викликами", purpose,
                exc_info=True,
            )
            raw = {}
        for key, value in raw.items():
            if isinstance(value, str):
                results[key] = LlmResponse(text=value, provider=route.provider, model=route.model)

    # Те, що не дав пакет (або пакета не було) — звичайними викликами.
    for key, prompt in items:
        if key in results:
            continue
        try:
            results[key] = call_llm(prompt, system_prompt, purpose=purpose)
        except LlmError as error:
            results[key] = error
    return results


def strip_code_fence(raw_content: str) -> str:
    """Знімає markdown-огорожу ```json ... ``` навколо відповіді.

    Живий кейс першого ж виклику Anthropic (2026-10-05): Opus 5 віддав
    коректний JSON, але в огорожі — і розбір упав на першому символі.
    DeepSeek такого не робив, бо викликається з
    `response_format={"type": "json_object"}`, якого в Anthropic немає
    (формат там або в промпті, або через structured outputs).

    Робиться в СПІЛЬНОМУ парсері, а не в Anthropic-клієнті: огорожа —
    властивість відповіді МОДЕЛІ, не транспорту, і будь-який провайдер
    може почати так відповідати після зміни промпту."""
    text = raw_content.strip()
    if not text.startswith("```"):
        return text

    lines = text.split("\n")
    # Перший рядок — сама огорожа з можливою назвою мови ("```json").
    lines = lines[1:]
    # Останній рядок — закривна огорожа (якщо модель її не обрізала).
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_json_object(raw_content: str, error_class: type = SynthesisResponseError) -> dict:
    """JSON-об'єкт із сирої відповіді LLM. `error_class` — щоб
    скрипт з іншою формою виходу (`discovery/news_tickers.py`) кидав свій
    власний тип помилки, не синтезний."""
    raw_content = strip_code_fence(raw_content)
    try:
        data = json.loads(raw_content)
    except json.JSONDecodeError as e:
        raise error_class(f"Відповідь LLM не є коректним JSON: {raw_content!r}") from e
    if not isinstance(data, dict):
        raise error_class(f"Відповідь LLM не є JSON-об'єктом: {raw_content!r}")
    return data


def parse_confidence(value, error_class: type = SynthesisResponseError) -> float:
    """confidence як число в [0,1] — спільна валідація для синтезу й
    для класифікації окремої статті (`relevance_filter.py`)."""
    try:
        confidence = float(value)
    except (TypeError, ValueError) as e:
        raise error_class(f"confidence не число: {value!r}") from e
    if not 0 <= confidence <= 1:
        raise error_class(f"confidence поза межами [0,1]: {confidence}")
    return confidence


def parse_synthesis_response(raw_content: str) -> SynthesisResult:
    """Розбір структурованого висновку синтезу — спільний для
    news_analysis/synthesize.py, news_analysis/synthesize_market.py і
    expectations/synthesize.py (усі троє мають ІДЕНТИЧНИЙ контракт
    виходу, різняться лише промптом)."""
    data = parse_json_object(raw_content)

    missing = [f for f in SYNTHESIS_FIELDS if f not in data]
    if missing:
        raise SynthesisResponseError(f"У відповіді LLM бракує полів {missing}: {data!r}")

    direction = data["direction"]
    if direction not in DIRECTIONS:
        raise SynthesisResponseError(f"Неочікуване значення direction: {direction!r}")

    return SynthesisResult(
        direction=direction,
        confidence=parse_confidence(data["confidence"]),
        summary=data["summary"],
        reasoning=data["reasoning"],
    )


def log_llm_call(
    conn,
    purpose: str,
    prompt: str,
    source_ref: Optional[str],
    llm: Optional[LlmResponse] = None,
    provider: Optional[str] = None,
    response: Optional[str] = None,
    model: Optional[str] = None,
    fallback_from: Optional[str] = None,
) -> int:
    """Обов'язковий аудит-лог виклику LLM (rule 5 кореневого CLAUDE.md:
    промпт + відповідь + timestamp + джерело).

    SQL живе тут, а не в `<пакет>/_db.py`, бо `llm_call_log` — не
    таблиця конкретного пакета, а спільний журнал ВСІХ LLM-викликів
    проєкту: раніше один і той самий INSERT був продубльований у
    `news_analysis/_db.py` і `expectations/_db.py`. Один писар — одна
    копія SQL.

    `model`/`fallback_from` (2026-10-05) — без них неможливо ні
    порівняти провайдерів на тих самих даних, ні побачити, що виклик
    тихо пішов на резервну модель.

    Передавати краще `llm=` (готовий `LlmResponse` із `call_llm()`) —
    тоді провайдер/модель/резерв беруться з ФАКТИЧНОГО виклику, а не
    з очікування викликача. Явні `provider`/`response` лишились для
    тестів і для коду, що формує рядок сам."""
    if llm is not None:
        provider = llm.provider
        model = model or llm.model
        fallback_from = fallback_from or llm.fallback_from
        response = response if response is not None else llm.text
    if provider is None or response is None:
        raise ValueError("log_llm_call: потрібен або llm=, або provider= і response=")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO llm_call_log
                (provider, purpose, prompt, response, source_ref, model, fallback_from)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (provider, purpose, prompt, response, source_ref, model, fallback_from),
        )
        llm_call_id = cur.fetchone()[0]
    conn.commit()
    return llm_call_id
