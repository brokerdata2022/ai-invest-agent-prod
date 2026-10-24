"""
Тонкий клієнт Anthropic Messages API — тільки мережевий виклик, без
інтерпретації відповіді (той самий принцип, що
`news_analysis/deepseek_client.py`: парсинг у структурований формат —
окремо, щоб тестуватись без мережі).

Чому не в `news_analysis/`, де лежить deepseek-клієнт: той опинився
там історично (перший LLM-виклик проєкту був саме аналізом новин), а
провайдер — не новинна річ. Новий клієнт поруч із `llm_common.py`,
тобто з тим єдиним місцем, яке вибирає провайдера.

## Три відмінності від DeepSeek, які НЕ можна скопіювати 1:1

1. **`temperature` НЕ надсилається.** На Opus 5 / Sonnet 5 параметри
   семплінгу (`temperature`/`top_p`/`top_k`) прибрані й повертають
   400. У DeepSeek-клієнті `temperature=0.0` — там це коректно.
2. **JSON не задається параметром `response_format`.** Формат
   вимагається в SYSTEM_PROMPT (так само, як уже написано в кожному
   нашому промпті: "Відповідай ЛИШЕ JSON-об'єктом"). Живий наслідок,
   виявлений першим же викликом: Opus 5 обертає відповідь у
   markdown-огорожу ```json — тому `llm_common.strip_code_fence()`
   знімає її перед розбором (у спільному парсері, бо це властивість
   відповіді моделі, не транспорту). Свідомо НЕ беремо
   `output_config.format` (structured outputs) у першій версії:
   бектест має порівнювати однакові промпти, а не два різні контракти
   виходу.
3. **Розмірковування увімкнене** (`thinking: adaptive` — на Opus 5 це
   стан за замовчуванням), а його токени тарифікуються як ВИХІДНІ.
   Тому глибина керується `output_config.effort` з `config.py`
   (розділ 9) — це головний важіль вартості, і `max_tokens` має бути
   щедрим, інакше відповідь обріжеться на півслові.
"""

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class AnthropicError(RuntimeError):
    pass


def call_anthropic(
    prompt: str,
    api_key: str,
    system_prompt: Optional[str] = None,
    model: str = "claude-sonnet-5",
    effort: Optional[str] = None,
    max_tokens: int = 4096,
    client=None,
    thinking: Optional[str] = None,
) -> str:
    """Викликає Anthropic Messages API, повертає сирий текст відповіді
    (склеєні всі text-блоки). `client` — для тестів без мережі.

    SDK сам ретраїть 429/5xx/мережеві збої (дефолт 2 спроби); ширший
    резерв — підміна провайдера в `llm_common.call_llm()`."""
    if client is None:
        # Імпорт усередині функції, не на рівні модуля: тести
        # маршрутизації й розбору відповіді не повинні вимагати
        # встановленого SDK (той самий підхід, що решта адаптерів
        # проєкту — мережева залежність тільки на справжньому виклику).
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

    request = build_request(prompt, system_prompt, model, effort, max_tokens, thinking)
    response = client.messages.create(**request)
    return extract_text(response, model)


def build_request(
    prompt: str,
    system_prompt: Optional[str],
    model: str,
    effort: Optional[str],
    max_tokens: int,
    thinking: Optional[str] = None,
) -> dict:
    """Тіло запиту Messages API — одне для звичайного виклику й для
    Batch API (там воно йде як `params` кожного запиту пакета).

    `thinking` (2026-10-24, налаштування користувача в `config.py`):
    None — поле не передається (модель думає за своїм дефолтом, як і
    до цієї зміни); "adaptive"/"disabled" — передається явно."""
    request: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system_prompt:
        request["system"] = system_prompt
    if effort:
        request["output_config"] = {"effort": effort}
    if thinking:
        request["thinking"] = {"type": thinking}
    return request


def extract_text(response, model: str) -> str:
    """Текст відповіді (склеєні text-блоки) або `AnthropicError` —
    спільне для звичайного виклику й результату пакета."""
    # Відмова класифікатора безпеки приходить як HTTP 200 зі
    # stop_reason="refusal" — без цієї перевірки код пішов би читати
    # порожній content і впав би незрозуміло.
    if getattr(response, "stop_reason", None) == "refusal":
        details = getattr(response, "stop_details", None)
        raise AnthropicError(
            f"Anthropic відмовив у запиті (stop_reason=refusal, "
            f"category={getattr(details, 'category', None)!r})"
        )

    texts = [
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text" and getattr(block, "text", "")
    ]
    if not texts:
        raise AnthropicError(
            f"Anthropic повернув відповідь без text-блоків "
            f"(stop_reason={getattr(response, 'stop_reason', None)!r}) — "
            "можливо, max_tokens вичерпано розмірковуванням"
        )

    usage = getattr(response, "usage", None)
    if usage is not None:
        logger.info(
            "Anthropic %s: in=%s out=%s токенів",
            model,
            getattr(usage, "input_tokens", "?"),
            getattr(usage, "output_tokens", "?"),
        )

    return "\n".join(texts)


def call_anthropic_batch(
    items: list[tuple[str, str]],
    api_key: str,
    system_prompt: Optional[str] = None,
    model: str = "claude-sonnet-5",
    effort: Optional[str] = None,
    max_tokens: int = 4096,
    thinking: Optional[str] = None,
    poll_seconds: int = 30,
    max_wait_seconds: int = 1500,
    client=None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Пакет запитів через Message Batches API — та сама відповідь за
    половину ціни, але асинхронно (2026-10-24, `filing_insight`).

    `items` — [(ключ викликача, промпт)]. Повертає {ключ: текст або
    `AnthropicError`} — помилка ОДНОГО запиту не валить решту пакета.

    Якщо пакет не завершився за `max_wait_seconds`, він скасовується і
    кидається `AnthropicError`: викликач тоді повторює звичайними
    викликами (`llm_common.call_llm_batch`), а не чекає добу. Дефолт
    25 хв — у межах 30-хвилинного таймауту джоби (`runner.py`).

    `custom_id` у API обмежений `[a-zA-Z0-9_-]{1,64}`, тож ключі
    викликача (тикери тощо) замінюються порядковими номерами."""
    if client is None:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

    keys = {f"r{index}": key for index, (key, _prompt) in enumerate(items)}
    requests_ = [
        {
            "custom_id": f"r{index}",
            "params": build_request(prompt, system_prompt, model, effort, max_tokens, thinking),
        }
        for index, (_key, prompt) in enumerate(items)
    ]
    batch = client.messages.batches.create(requests=requests_)
    logger.info("Anthropic batch %s: %d запитів відправлено", batch.id, len(requests_))

    waited = 0
    while getattr(batch, "processing_status", None) != "ended":
        if waited >= max_wait_seconds:
            try:
                client.messages.batches.cancel(batch.id)
            except Exception:
                logger.warning("Не вдалося скасувати batch %s", batch.id, exc_info=True)
            raise AnthropicError(
                f"batch {batch.id} не завершився за {max_wait_seconds} с — скасовано"
            )
        sleep(poll_seconds)
        waited += poll_seconds
        batch = client.messages.batches.retrieve(batch.id)

    results: dict[str, object] = {}
    for entry in client.messages.batches.results(batch.id):
        key = keys.get(entry.custom_id)
        if key is None:
            continue
        result = entry.result
        if getattr(result, "type", None) == "succeeded":
            try:
                results[key] = extract_text(result.message, model)
            except AnthropicError as error:
                results[key] = error
        else:
            results[key] = AnthropicError(
                f"запит пакета {entry.custom_id} ({key}): {getattr(result, 'type', None)}"
            )
    for key in keys.values():
        results.setdefault(key, AnthropicError(f"{key}: немає результату в пакеті"))
    logger.info("Anthropic batch %s: завершено за ~%d с", batch.id, waited)
    return results
