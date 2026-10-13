"""
Тонкий HTTP-клієнт DeepSeek chat completions — тільки мережевий виклик,
без інтерпретації відповіді (парсинг у структурований формат —
relevance_filter.parse_response(), окремо, щоб тестуватись без мережі).
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"


class DeepSeekError(RuntimeError):
    pass


def call_deepseek(
    prompt: str,
    api_key: str,
    system_prompt: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.0,
    session: Optional[requests.Session] = None,
) -> str:
    """Викликає DeepSeek chat completions, повертає сирий текст відповіді
    (content першого choice). Відповідь запитуємо в JSON-форматі
    (response_format), щоб не парсити вільний текст."""
    session = session or requests.Session()
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    response = session.post(
        DEEPSEEK_CHAT_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise DeepSeekError(f"Неочікувана відповідь DeepSeek: {data!r}") from e
