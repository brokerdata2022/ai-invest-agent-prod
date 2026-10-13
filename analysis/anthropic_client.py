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
from typing import Optional

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

    request: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system_prompt:
        request["system"] = system_prompt
    if effort:
        request["output_config"] = {"effort": effort}

    response = client.messages.create(**request)

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
