"""
Спільний HTTP-хелпер для надсилання повідомлень у Telegram — виніс із
telegram_notify.py/news_notify.py (мали буквально ідентичну функцію),
щоб orchestration/alerts.py міг перевикористати той самий код замість
третьої копії.

`parse_mode` (2026-10-03, рішення користувача — гарне форматування
усіх повідомлень): необов'язковий, за замовчуванням None (як і було —
plain text, без жодної інтерпретації). `orchestration/alerts.py`
свідомо лишається на дефолті (ops-алерти про провал джоби мають
надійти, навіть якщо текст випадково містить "<"/"&" — з parse_mode
"HTML" такий символ дав би 400 Bad Request і зламав сам алерт);
reporting/-скрипти, що будують HTML (`_common.py:escape_html/bold/
link`), передають `parse_mode="HTML"` явно.

`get_updates()`/`set_my_commands()` (2026-10-03, Telegram-команди
людською мовою — `orchestration/telegram_commands.py`): той самий
принцип — тонка HTTP-обгортка, жодної логіки парсингу/диспетчеризації
команд тут (вона в samому telegram_commands.py)."""

import logging
import os

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"


# Середовища, з яких НЕ можна писати живому користувачу. Перевіряється
# в єдиній точці відправки — тут, а не в кожному notify-скрипті
# (їх 30, і забути один означало б лишити дірку).
NON_PRODUCTION_ENVS = frozenset({"dev", "development", "test", "local", "staging"})


def is_sending_blocked() -> str | None:
    """Назва середовища, якщо відправка заборонена, інакше None.

    **Навіщо (живий випадок 2026-10-16).** Дев-стек і продакшен
    використовують ОДИН токен бота. Користувач отримав у Telegram
    відповідь, зібрану з дев-бази — відновленого знімка від 13.10 — і
    вона була зовні невідрізнима від продакшенівської: ті самі
    формулювання, той самий бот. Розбір показав, що запис ішов із
    дев-контейнера, і це означає, що будь-який ручний прогін у деві
    може надіслати користувачу застарілу картину ринку.

    **Чому «блокувати лише явно позначене НЕ-продакшен», а не
    «дозволяти лише явно позначений продакшен».** Друге виглядає
    строгішим, але якби змінної не було в продакшен-`.env`, воно
    вимкнуло б УСІ сповіщення агента — тихо й одразу. Асиметрія
    навмисна: помилка в цей бік коштує «дев щось надіслав», у той —
    «агент замовк і ти про це не дізнався».
    """
    env = (os.environ.get("AGENT_ENV") or "").strip().lower()
    return env if env in NON_PRODUCTION_ENVS else None


def send_telegram_message(token: str, chat_id: str, text: str, parse_mode: str | None = None) -> dict:
    blocked_env = is_sending_blocked()
    if blocked_env:
        # Не виняток: прогін у деві має доходити до кінця (нам потрібен
        # результат), просто без доставки живому користувачу.
        logger.warning(
            "AGENT_ENV=%s — відправка в Telegram ПРОПУЩЕНА (%d символів). "
            "Так зроблено, щоб дев-прогін не надсилав користувачу дані "
            "дев-бази; у продакшені AGENT_ENV не задається.",
            blocked_env, len(text),
        )
        return {"ok": False, "skipped_env": blocked_env}

    url = TELEGRAM_API_BASE.format(token=token, method="sendMessage")
    data = {"chat_id": chat_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    response = requests.post(url, data=data, timeout=15)
    response.raise_for_status()
    return response.json()


def get_updates(token: str, offset: int | None = None, timeout: int = 0) -> list[dict]:
    """Короткий polling Telegram `getUpdates` — повертає список Update-
    об'єктів (може бути порожнім, якщо нових повідомлень немає).
    `offset` — update_id останнього вже обробленого +1 (Telegram сам
    більше не повертає підтверджені оновлення після цього)."""
    url = TELEGRAM_API_BASE.format(token=token, method="getUpdates")
    params: dict = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    response = requests.get(url, params=params, timeout=timeout + 10)
    response.raise_for_status()
    return response.json().get("result", [])


def set_my_commands(token: str, commands: list[dict]) -> dict:
    """`commands` — [{'command': 'job_name', 'description': '...'}].
    Реєструє "/"-меню бота в Telegram UI (назва+опис видно одразу при
    наборі "/"). Не щоразу з опитуванням — окремий ручний скрипт
    (`orchestration/register_telegram_commands.py`), бо список команд
    змінюється рідко (новий job)."""
    url = TELEGRAM_API_BASE.format(token=token, method="setMyCommands")
    response = requests.post(url, json={"commands": commands}, timeout=15)
    response.raise_for_status()
    return response.json()
